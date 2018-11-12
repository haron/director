import asyncio
import ujson
from prodict import Prodict as pdict
from itertools import count

from band import logger, settings, rpc, app, scheduler
from band.constants import (
    NOTIFY_ALIVE, REQUEST_STATUS, OK, FRONTIER_SERVICE,
    DIRECTOR_SERVICE)

from ..helpers import nn, merge_dicts
from ..band_config import BandConfig
from ..constants import (
    STARTED_SET, SERVICE_TIMEOUT, DEFAULT_COL, DEFAULT_ROW,
    STATUS_RESTARTING, STATUS_REMOVING, STATUS_STARTING,
    STATUS_STOPPING, SHARED_CONFIG_KEY)

from ..docker_manager import DockerManager
from .context import StateCtx
from .service import ServiceState
from ..image_navigator import ImageNavigator

image_navigator = ImageNavigator(**settings)
band_config = BandConfig(**settings)
dock = DockerManager(image_navigator=image_navigator, **settings)


class StateManager:
    def __init__(self):
        self.cols = 6
        self.rows = 6
        self.timeout = 30
        self._state = dict()
        self._dock = None
        self._shared_config = dict()
        self.registrations_hash = ''

    """
    Lifecycle functions
    """

    async def initialize(self):
        await band_config.initialize()
        await image_navigator.load()
        await self.load_config(SHARED_CONFIG_KEY)
        await self.resolve_docstatus_all()
        
        # initial fill autostart 
        started_present = await band_config.set_exists(STARTED_SET)
        if not started_present:
            await band_config.set_add(STARTED_SET, *settings.initial_startup)

        # looking for containers to request status
        for container in await dock.containers(struct=list):
            if container.running and container.native:
                await scheduler.spawn(
                    self.request_app_state(container.name))
        
        # spawning state cleaner job
        await scheduler.spawn(self.clean_worker())

        await scheduler.spawn(self.images_loader())

        # handling autostart
        await self.handle_auto_start()

    async def images_loader(self):
        while True:
            await asyncio.sleep(15)
            try:
                await image_navigator.load()
            except Exception:
                logger.exception('ex')

    async def clean_worker(self):
        while True:
            # Remove expired services
            try:
                await asyncio.sleep(5)
                await self.resolve_docstatus_all()
                await self.check_regs_changed()
            except ConnectionRefusedError:
                logger.error('Redis connection refused')
            except asyncio.CancelledError:
                logger.warn('Asyncio cancelled')
                break
            except Exception:
                logger.exception('state initialize')
            await asyncio.sleep(1)

    async def handle_auto_start(self):
        services = await self.should_start()
        logger.info("Autostarting services", items=services)
        for item in services:
            svc = await self.get(item)
            if not svc.is_active() and image_navigator.is_native(svc.name):
                await self.run_service(svc.name)
            # if not (item in state and ().is_active()):
            # asyncio.ensure_future(run(item))

    async def unload(self):
        await band_config.unload()
        await dock.close()

    """
    State functions
    """

    @property
    def state(self):
        return self._state

    def values(self):
        return self._state.values()

    def __contains__(self, name):
        return self.is_exists(name)

    def is_exists(self, name):
        return name in self._state

    """
    Container management functions
    """

    async def get(self, name, **kwargs):
        params = kwargs.pop('params', pdict())
        wanted_pos = None
        envs = []

        if params.pos and params.pos.col and params.pos.row:
            wanted_pos = dict(col=params.pos.col, row=params.pos.row)

        if name not in self._state:
            logger.debug('loading state', name=name)
            config = await self.load_config(name)
            meta = await image_navigator.image_meta(name)

            if meta and meta.env:
                envs.append(meta.env)

            if config and config.env:
                envs.append(config.env)

            svc = ServiceState(name=name, manager=self)

            if meta:
                svc.set_meta(meta)

            if not wanted_pos and config and 'pos' in config:
                if nn(config.pos.col) and nn(config.pos.row):
                    wanted_pos = config.pos

            if not wanted_pos and meta and 'pos' in meta:
                if nn(meta.pos.col) and nn(meta.pos.row):
                    wanted_pos = meta.pos

            if not wanted_pos:
                wanted_pos = dict(col=DEFAULT_COL, row=DEFAULT_ROW)

            self._state[name] = svc

        svc = self._state[name]

        # Container env
        if params.env:
            envs.append(params.env)

        if len(envs):
            svc.set_env(merge_dicts(*envs))

        if params.build_options:
            svc.set_build_opts(**params['build_options'])

        if wanted_pos:
            pos = self._allocate(name, **wanted_pos)
            svc.set_pos(**pos)

        return svc

    async def run_service(self, name, no_wait=False):
        svc = await self.get(name)
        svc.clean_status()
        svc.set_status_override(STATUS_STARTING)
        coro = self._do_run_service(name)
        await (scheduler.spawn(coro) if no_wait else coro)
        return svc

    async def _do_run_service(self, name):
        svc = await self.get(name)
        env = self._shared_config.get('env', {}).copy()
        env.update(svc.env)
        await dock.run_container(name, env=env, **svc.build_options)
        await band_config.set_add(STARTED_SET, name)
        logger.debug('svc', svc=dict(bo=svc.build_options, e=svc.env))
        logger.debug('saving config')
        svc.save_config()
        logger.debug('resolving svc status')
        await self.resolve_docstatus(name)

    async def remove_service(self, name, no_wait=False):
        svc = await self.get(name)
        await band_config.set_rm(STARTED_SET, name)
        svc.set_status_override(STATUS_REMOVING)
        coro = self._do_remove_service(name)
        await (scheduler.spawn(coro) if no_wait else coro)
        return svc

    async def _do_remove_service(self, name):
        svc = await self.get(name)
        await dock.remove_container(name)
        svc.clean_status()

    async def stop_service(self, name, no_wait=False):
        svc = await self.get(name)
        await band_config.set_rm(STARTED_SET, name)
        svc.set_status_override(STATUS_STOPPING)
        coro = self._do_stop_service(name)
        await (scheduler.spawn(coro) if no_wait else coro)
        return svc

    async def _do_stop_service(self, name):
        svc = await self.get(name)
        await dock.stop_container(name)
        svc.clean_status()

    async def start_service(self, name, no_wait=False):
        svc = await self.get(name)
        if svc.native:
            await band_config.set_add(STARTED_SET, name)
        svc.set_status_override(STATUS_STARTING)
        coro = self._do_start_service(name)
        await (scheduler.spawn(coro) if no_wait else coro)
        return svc

    async def _do_start_service(self, name):
        svc = await self.get(name)
        await dock.start_container(name)
        svc.clean_status()

    async def restart_service(self, name, no_wait=False):
        svc = await self.get(name)
        svc.set_status_override(STATUS_RESTARTING)
        coro = self._do_restart_service(name)
        await (scheduler.spawn(coro) if no_wait else coro)
        return svc

    async def _do_restart_service(self, name):
        container = await dock.get(name)
        svc = await self.get(name)
        if container:
            svc.clean_status()
            await dock.restart_container(name)
            svc.clean_status()
            await self.check_regs_changed()

    """
    State functions
    """

    async def resolve_docstatus(self, name):
        svc = await self.get(name)
        container = await dock.get(name)
        if container:
            svc.set_dockstate(container.full_state())

    async def resolve_docstatus_all(self):
        for container in await dock.containers(struct=list):
            await self.resolve_docstatus(container.name)

    async def clean_status(self, name):
        (await self.get(name)).clean_status()

    async def request_app_state(self, name):
        svc = await self.get(name)
        # Service-dependent payload send with status request
        payload = dict()
        # Payload for frontend servoce
        if name == FRONTIER_SERVICE:
            payload.update(self.registrations())
            payload.update(dict(state_hash=self.registrations_hash))

        # Loading state, config, meta
        status = await rpc.request(name, REQUEST_STATUS, **payload)
        svc.set_appstate(status)

    async def check_regs_changed(self):
        new_hash = hash(ujson.dumps(self.registrations()))
        # If registrations changed front shold know about that
        if new_hash != self.registrations_hash:
            self.registrations_hash = new_hash
            await self.request_app_state(FRONTIER_SERVICE)

    def registrations(self):
        methods = []
        for svc in self.values():
            if svc.is_active():
                for method in svc.methods:
                    methods.append(method)
        return dict(register=methods)

    def clean_ctx(self, name, coro):
        return StateCtx(self, name, coro)

    """
    Config store functions
    """

    async def configs(self):
        return await band_config.configs_list()

    async def load_config(self, name):
        config = await band_config.load_config(name)
        if name == SHARED_CONFIG_KEY and config:
            self._shared_config = config
        logger.debug('loaded config', name=name, config=config)
        return config

    def save_config(self, name, config):
        logger.info('saving', name=name, config=config)
        job = scheduler.spawn(band_config.save_config(name, config))
        asyncio.ensure_future(job)

    async def runned_set(self):
        return await band_config.set_get(STARTED_SET)

    async def update_config(self, name, keysvals):
        config = (await self.load_config(name)) or pdict()
        for k, v in keysvals.items():
            target = config
            path = k.split('.')
            prop = path.pop()
            for p in path:
                target = target[p]
            if v == '':
                target.pop(prop, None)
            else:
                target[prop] = v
        self.save_config(name, config)
        if name == SHARED_CONFIG_KEY:
            self._shared_config = config
        return config

    async def should_start(self):
        return await band_config.set_get(STARTED_SET)

    """
    Dashboard tile
    """

    def _allocate(self, name, col, row):
        """
        Allocating dashboard position for container close to wanted
        """
        occupied = self._occupied(exclude=name)
        for icol, irow in self._space_walk(int(col), int(row)):
            key = f"{icol}x{irow}"
            if key not in occupied:
                logger.debug(f'Allocatted position', name=name, pos=f"{col}x{row}", occupied=occupied, allocated=f"{icol}x{irow}")
                return dict(col=icol, row=irow)

    def _occupied(self, exclude=None):
        """
        Building list of occupied positions
        """
        occupied = []
        for srv in self._state.values():
            if srv.name != exclude and nn(srv.pos.col) and nn(srv.pos.row):
                occupied.append(srv.pos.to_s())
        return occupied

    def _space_walk(self, scol=0, srow=0):
        """
        Generator over all pissible postions starting from specified location
        """
        srow = int(srow)
        scol = int(scol)
        # first part
        for rowi in range(srow, self.rows):
            for coli in range(scol, self.cols):
                yield coli, rowi
        # back side
        for rowi in range(0, srow):
            for coli in range(0, scol):
                yield coli, rowi
