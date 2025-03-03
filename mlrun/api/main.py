import asyncio
import concurrent.futures
import traceback
import uuid

import fastapi
import fastapi.concurrency
import uvicorn
import uvicorn.protocols.utils
from fastapi.exception_handlers import http_exception_handler

import mlrun.api.schemas
import mlrun.api.utils.clients.chief
import mlrun.errors
import mlrun.utils
import mlrun.utils.version
from mlrun.api.api.api import api_router
from mlrun.api.db.session import close_session, create_session
from mlrun.api.initial_data import init_data
from mlrun.api.utils.periodic import (
    cancel_all_periodic_functions,
    run_function_periodically,
)
from mlrun.api.utils.singletons.db import get_db, initialize_db
from mlrun.api.utils.singletons.logs_dir import initialize_logs_dir
from mlrun.api.utils.singletons.project_member import (
    get_project_member,
    initialize_project_member,
)
from mlrun.api.utils.singletons.scheduler import get_scheduler, initialize_scheduler
from mlrun.config import config
from mlrun.k8s_utils import get_k8s_helper
from mlrun.runtimes import RuntimeKinds, get_runtime_handler
from mlrun.utils import logger

API_PREFIX = "/api"
BASE_VERSIONED_API_PREFIX = f"{API_PREFIX}/v1"


app = fastapi.FastAPI(
    title="MLRun",
    description="Machine Learning automation and tracking",
    version=config.version,
    debug=config.httpdb.debug,
    # adding /api prefix
    openapi_url=f"{BASE_VERSIONED_API_PREFIX}/openapi.json",
    docs_url=f"{BASE_VERSIONED_API_PREFIX}/docs",
    redoc_url=f"{BASE_VERSIONED_API_PREFIX}/redoc",
    default_response_class=fastapi.responses.ORJSONResponse,
)
app.include_router(api_router, prefix=BASE_VERSIONED_API_PREFIX)
# This is for backward compatibility, that is why we still leave it here but not include it in the schema
# so new users won't use the old un-versioned api
# TODO: remove when 0.9.x versions are no longer relevant
app.include_router(api_router, prefix=API_PREFIX, include_in_schema=False)


@app.exception_handler(Exception)
async def generic_error_handler(request: fastapi.Request, exc: Exception):
    error_message = repr(exc)
    return await fastapi.exception_handlers.http_exception_handler(
        # we have no specific knowledge on what was the exception and what status code fits so we simply use 500
        # This handler is mainly to put the error message in the right place in the body so the client will be able to
        # show it
        # TODO: 0.6.6 is the last version expecting the error details to be under reason, when it's no longer a relevant
        #  version can be changed to detail=error_message
        request,
        fastapi.HTTPException(status_code=500, detail={"reason": error_message}),
    )


@app.exception_handler(mlrun.errors.MLRunHTTPStatusError)
async def http_status_error_handler(
    request: fastapi.Request, exc: mlrun.errors.MLRunHTTPStatusError
):
    status_code = exc.response.status_code
    error_message = repr(exc)
    logger.warning(
        "Request handling returned error status",
        error_message=error_message,
        status_code=status_code,
        traceback=traceback.format_exc(),
    )
    # TODO: 0.6.6 is the last version expecting the error details to be under reason, when it's no longer a relevant
    #  version can be changed to detail=error_message
    return await http_exception_handler(
        request,
        fastapi.HTTPException(
            status_code=status_code, detail={"reason": error_message}
        ),
    )


def get_client_address(scope):
    # uvicorn expects this to be a tuple while starlette test client sets it to be a list
    if isinstance(scope.get("client"), list):
        scope["client"] = tuple(scope.get("client"))
    return uvicorn.protocols.utils.get_client_addr(scope)


@app.middleware("http")
async def log_request_response(request: fastapi.Request, call_next):
    request_id = str(uuid.uuid4())
    silent_logging_paths = [
        "healthz",
    ]
    path_with_query_string = uvicorn.protocols.utils.get_path_with_query_string(
        request.scope
    )
    if not any(
        silent_logging_path in path_with_query_string
        for silent_logging_path in silent_logging_paths
    ):
        logger.debug(
            "Received request",
            method=request.method,
            client_address=get_client_address(request.scope),
            http_version=request.scope["http_version"],
            request_id=request_id,
            uri=path_with_query_string,
        )
    try:
        response = await call_next(request)
    except Exception as exc:
        logger.warning(
            "Request handling failed. Sending response",
            # User middleware (like this one) runs after the exception handling middleware, the only thing running after
            # it is Starletter's ServerErrorMiddleware which is responsible for catching any un-handled exception
            # and transforming it to 500 response. therefore we can statically assign status code to 500
            status_code=500,
            request_id=request_id,
            uri=path_with_query_string,
            method=request.method,
            exc=exc,
            traceback=traceback.format_exc(),
        )
        raise
    else:
        if not any(
            silent_logging_path in path_with_query_string
            for silent_logging_path in silent_logging_paths
        ):
            logger.debug(
                "Sending response",
                status_code=response.status_code,
                request_id=request_id,
                uri=path_with_query_string,
                method=request.method,
            )
        return response


@app.on_event("startup")
async def startup_event():
    logger.info(
        "configuration dump",
        dumped_config=config.dump_yaml(),
        version=mlrun.utils.version.Version().get(),
    )
    loop = asyncio.get_running_loop()
    loop.set_default_executor(
        concurrent.futures.ThreadPoolExecutor(
            max_workers=int(config.httpdb.max_workers)
        )
    )

    initialize_logs_dir()
    initialize_db()

    if config.httpdb.state == mlrun.api.schemas.APIStates.online:
        await move_api_to_online()


@app.on_event("shutdown")
async def shutdown_event():
    if get_project_member():
        get_project_member().shutdown()
    cancel_all_periodic_functions()
    if get_scheduler():
        await get_scheduler().stop()


async def move_api_to_online():
    logger.info("Moving api to online")
    initialize_project_member()
    await initialize_scheduler()
    # runs cleanup/monitoring is not needed if we're not inside kubernetes cluster
    # periodic functions should only run on the chief instance
    if (
        get_k8s_helper(silent=True).is_running_inside_kubernetes_cluster()
        and config.httpdb.clusterization.role
        == mlrun.api.schemas.ClusterizationRole.chief
    ):
        _start_periodic_cleanup()
        _start_periodic_runs_monitoring()


def _start_periodic_cleanup():
    interval = int(config.runtimes_cleanup_interval)
    if interval > 0:
        logger.info("Starting periodic runtimes cleanup", interval=interval)
        run_function_periodically(
            interval, _cleanup_runtimes.__name__, False, _cleanup_runtimes
        )


def _start_periodic_runs_monitoring():
    interval = int(config.runs_monitoring_interval)
    if interval > 0:
        logger.info("Starting periodic runs monitoring", interval=interval)
        run_function_periodically(
            interval, _monitor_runs.__name__, False, _monitor_runs
        )


def _monitor_runs():
    db_session = create_session()
    try:
        for kind in RuntimeKinds.runtime_with_handlers():
            try:
                runtime_handler = get_runtime_handler(kind)
                runtime_handler.monitor_runs(get_db(), db_session)
            except Exception as exc:
                logger.warning(
                    "Failed monitoring runs. Ignoring", exc=str(exc), kind=kind
                )
    finally:
        close_session(db_session)


def _cleanup_runtimes():
    db_session = create_session()
    try:
        for kind in RuntimeKinds.runtime_with_handlers():
            try:
                runtime_handler = get_runtime_handler(kind)
                runtime_handler.delete_resources(get_db(), db_session)
            except Exception as exc:
                logger.warning(
                    "Failed deleting resources. Ignoring", exc=str(exc), kind=kind
                )
    finally:
        close_session(db_session)


def main():
    init_data()
    logger.info(
        "Starting API server",
        port=config.httpdb.port,
        debug=config.httpdb.debug,
    )
    uvicorn.run(
        "mlrun.api.main:app",
        host="0.0.0.0",
        port=config.httpdb.port,
        debug=config.httpdb.debug,
        access_log=False,
    )


if __name__ == "__main__":
    main()
