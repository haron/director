import asyncio
import ujson
from band import expose, logger, settings
from simplech import AsyncClickHouse
from .. import stat_queries

ch = AsyncClickHouse()


@expose()
async def common_stat(**params):
    where = stat_queries.events_where()
    query = stat_queries.groups(where) + stat_queries.FMT_JSON
    logger.debug(query)
    stat_groups = await ch.select(query)
    if stat_groups:
        return ujson.loads(stat_groups)['data']
    return {}


@expose()
async def events_stat(**params):
    events_where = stat_queries.events_where()
    query = stat_queries.events(events_where) + stat_queries.FMT_JSON
    logger.debug(query.replace('\n', ' '))
    stat_events = await ch.select(query)
    return ujson.loads(stat_events)['data'] if stat_events else []
