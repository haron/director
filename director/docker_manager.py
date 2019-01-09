from pathlib import Path
from collections import namedtuple
import aiofiles
import asyncio
import aiodocker
from aiodocker.exceptions import DockerError
from aiodocker.logs import DockerLog
from aiodocker.channel import Channel, ChannelSubscriber
from aiodocker.containers import DockerContainer
import os
import sys
import stat
import re
import codecs
from prodict import Prodict as pdict
from time import time
import ujson
import subprocess
from typing import Set, List, Dict
from pprint import pprint
from band import logger, scheduler, loop
from .image_navigator import ImageNavigator
from .band_container import BandContainer, BandContainerBuilder
from .constants import DEF_LABELS, STATUS_RUNNING
from .helpers import req_to_bool, def_val
import struct


class DockerManager():
    image_navigator: ImageNavigator
    reserved_ports: Set
    container_params: pdict

    """
    Useful links:
    http://aiodocker.readthedocs.io/en/latest/
    https://docs.docker.com/engine/api/v1.37/#operation/ContainerList
    https://docs.docker.com/engine/api/v1.24/#31-containers
    """

    def __init__(self,
                 images,
                 container_params,
                 image_params,
                 image_navigator,
                 start_port=8900,
                 end_port=8999,
                 **kwargs):
        # instance of low-level async docker client
        self.dc = aiodocker.Docker()
        # containers images navigator
        self.image_navigator = image_navigator
        # pool start port
        self.start_port = start_port
        # pool end port
        self.end_port = end_port
        # ports reservation
        self.reserved_ports = set()
        # common container params
        self.container_params = pdict.from_dict(container_params)
        self.image_params = pdict.from_dict(image_params)
        # start load images
        # asyncio.ensure_future(self.initialize())

    async def initialize(self):
        self.logs = Channel()
        coro = self.events_reader(self.dc, self.logs)
        await scheduler.spawn(coro)

    def get_log_reader(self):
        return self.logs.subscribe()

    async def logs_reader(self, docker, container: DockerContainer, channel: Channel, name, cid):
        logger = container.logs
        subscriber = logger.subscribe()
        now = int(time())
        await scheduler.spawn(logger.run(since=now))
        while True:
            log_record = await subscriber.get()
            if log_record is None:
                print('closing docker logs reader')
                # decoded = decoder.decode(b'', final=True)
                # if decoded:
                    # print(decoded)
                break
            # decoded = decoder.decode()
            # https://ahmet.im/blog/docker-logs-api-binary-format-explained/
            now = int(time() * 1000)
            mv = memoryview(log_record)
            data = bytes(mv[8:]).decode('utf-8', 'replace')
            source = mv[0]
            size = struct.unpack('>L', mv[4:8])[0]            
            msg = (now, cid, name, source, size, data)
            await channel.publish(msg)

    async def events_reader(self, docker, logs):
        subscriber = docker.events.subscribe()

        for bc in await self.containers(inband=False, status='running', struct=list):
            container = bc.container
            await scheduler.spawn(self.logs_reader(docker, container, logs, bc.name, bc.id))
            logger.debug(f'creating logger for {bc.name}')

        while True:
                event = await subscriber.get()
                if event is None:
                    break
                if event['Action'] == 'start' and event['Type'] == 'container':
                    cid = event['Actor']['ID']
                    container = await docker.containers.get(cid)
                    name = event['Actor']['Attributes']['name']
                    pprint(event)
                    await scheduler.spawn(self.logs_reader(docker, container, logs, name, cid))
                if event['Action'] == 'stop' and event['Type'] == 'container':
                    pprint(event)
		# case "start", "restart":
		# 	go p.pumpLogs(event, backlog(), inactivityTimeout)
		# case "rename":
		# 	go p.rename(event)
		# case "die":
		# 	go p.update(event)
                # Demonstrate simple event-driven container mgmt.
                # if event['Actor']['ID'] == container._id:
                    # if event['Action'] == 'start':
                    #     await container.stop()
                    #     print("=> killed {}".format(container._id[:12]))
                    # elif event['Action'] == 'stop':
                    #     await container.delete(force=True)
                    #     print("=> deleted {}".format(container._id[:12]))
                    # elif event['Action'] == 'destroy':
                    #     print('=> done with this container!')
                    #     break


    async def containers(self, struct=dict, status=None, fullinfo=False, inband=True):
        filters = pdict()
        if inband:
            filters.label = ['inband']
        if status:
            filters.status = [status]
        containers = await self.dc.containers.list(
            all=True, filters=ujson.dumps(filters))
        lst = list(BandContainer(c) for c in containers)
        return lst if struct == list else {c.name: c for c in lst}

    async def conts_list(self):
        conts = await self.containers()
        return [c.short_info for c in conts.values()]

    async def get(self, name):
        try:
            container = await self.dc.containers.get(name)
            if container:
                return BandContainer(container)
        except DockerError as e:
            logger.warn("Fetched exception",
                        status=e.status, message=e.message)

        # return (await self.containers()).get(name, None)

    async def available_ports(self):
        available_ports = set(range(self.start_port, self.end_port))
        conts = await self.containers()
        used_ports = set(
            sum(
                list(cont.ports for cont in conts.values()),
                []
            )
        )
        logger.info(f"checking used ports {used_ports}")
        return available_ports - used_ports - self.reserved_ports

    def hold_ports(self, ports):
        for port in ports:
            self.reserved_ports.add(port)

    def free_ports(self, ports):
        for port in ports:
            self.reserved_ports.add(port)

    async def remove_container(self, name):
        # removing if running
        try:
            container = await self.get(name)
            if container:
                await container.fill()
                if container.state == 'running':
                    logger.info("Stopping container")
                    await container.stop()
                    if not container.auto_removable():
                        await container.delete()
                else:
                    await container.delete()

                try:
                    await container.wait(condition="removed")
                except DockerError as e:
                    logger.debug('Docker 404 received on wait request')
                    if e.status != 404:
                        raise e

        except DockerError:
            logger.exception('container remove exc')
        return True

    async def stop_container(self, name):
        conts = await self.containers()
        if name in conts:
            logger.info(f"stopping container {name}")
            await conts[name].stop()
            return True

    async def start_container(self, name):
        conts = await self.containers()
        if name in conts:
            logger.info(f"starting container {name}")
            await conts[name].start()
            return True

    async def restart_container(self, name):
        conts = await self.containers()
        if name in conts:
            logger.info(f"restarting container {name}")
            await conts[name].restart()
            return True

    async def create_image(self, img, img_options):
        logger.debug(
            f">>> Building image {img.name} from {img.path}. img_options",
            img_options=img_options)
        async with img.create(img_options) as builder:
            progress = pdict()
            struct = builder.struct()
            last = time()
            async for chunk in await self.dc.images.build(**struct):
                if isinstance(chunk, dict):
                    chunk = pdict.from_dict(chunk)
                    if chunk.aux:
                        struct.id = chunk.aux.ID
                        logger.debug('chunk', chunk=chunk)
                    elif chunk.status and chunk.id:
                        progress[chunk.id] = chunk
                        if time() - last > 1:
                            logger.info("\nDocker build progress", progress=progress)
                            last = time()
                    elif chunk.stream:
                        # logger.debug('chunk', chunk=chunk)
                        step = re.search(r'Step\s(\d+)\/(\d+)', chunk.stream)
                        if step:
                            logger.debug('Docker build step ', groups=step.groups())
                    else:
                        logger.debug('unknown chunk', chunk=chunk)
                else:
                    logger.debug('unknown chunk type', type=type(chunk), chunk=chunk)
            logger.info('Docker image created', struct_id=struct.id)
            return img.set_data(await self.dc.images.get(img.name))

    async def run_container(self, name, env={}, nocache=None, auto_remove=None, **kwargs):

        image_options = dict(
            nocache=def_val(nocache, False),
            **self.image_params
        )
        container_options = dict(auto_remove=def_val(auto_remove, False))

        logger.info('called run container (kwargs will not used)', env=env,
                    func_args=dict(auto_remove=auto_remove, nocache=nocache, kwargs=kwargs), image_options=image_options, container_options=container_options)

        # building image
        service_img = self.image_navigator[name]
        await self.create_image(service_img, image_options)
        await self.remove_container(name)
        await asyncio.sleep(0.1)
        # preparing to run
        available_ports = await self.available_ports()
        allocated_ports = list(available_ports.pop()
                               for p in service_img.ports)
        self.hold_ports(allocated_ports)
        try:
            params = pdict.from_dict({
                **dict(host_ports=allocated_ports),
                **self.container_params})
            params.env.update(env)
            builder = BandContainerBuilder(service_img)
            config = builder.run_struct(name, **container_options, **params)
            # running service
            logger.info(f"starting container {name}.")
            dc = await self.dc.containers.run(config=config, name=name)
            c = BandContainer(dc)
            await c.ensure_filled()
            logger.info(f'started container {c.name} [{c.short_id}] {c.ports}')
            return c.short_info
        except Exception as exc:
            raise exc
        finally:
            self.free_ports(allocated_ports)

    async def close(self):
        await self.dc.close()
