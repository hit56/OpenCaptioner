"""Runtime hooks injected by gateway (or tests)."""

_logger = None
_get_llm_url = None
_get_embed_url = None


def configure(*, logger=None, get_llm_url=None, get_embed_url=None):
    global _logger, _get_llm_url, _get_embed_url
    if logger is not None:
        _logger = logger
    if get_llm_url is not None:
        _get_llm_url = get_llm_url
    if get_embed_url is not None:
        _get_embed_url = get_embed_url


def get_logger():
    if _logger is not None:
        return _logger
    import logging
    return logging.getLogger("subtitles")


def resolve_llm_url(session_id: str = "", route_source: str = "subtitles"):
    if _get_llm_url is not None:
        return _get_llm_url(route_source=route_source, session_id=session_id)
    import utils
    router = utils.ServiceURLRouter(utils.parse_llm_service_urls())
    return router.select(routing_key=session_id or None)["url"]


def resolve_embed_url(session_id: str = "", route_source: str = "rag"):
    if _get_embed_url is not None:
        return _get_embed_url(route_source=route_source, session_id=session_id)
    import utils
    urls = utils.parse_embed_service_urls()
    if not urls:
        raise RuntimeError("EMBED_URL 未配置，无法调用 embedding 服务")
    router = utils.ServiceURLRouter(urls)
    return router.select(routing_key=session_id or None)["url"]
