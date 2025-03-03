import fastapi
import semver
import sqlalchemy.orm

import mlrun.api.api.deps
import mlrun.api.schemas
import mlrun.api.utils.auth.verifier
import mlrun.api.utils.background_tasks
import mlrun.api.utils.clients.chief
from mlrun.utils import logger

router = fastapi.APIRouter()


@router.get(
    "/projects/{project}/background-tasks/{name}",
    response_model=mlrun.api.schemas.BackgroundTask,
)
def get_project_background_task(
    project: str,
    name: str,
    auth_info: mlrun.api.schemas.AuthInfo = fastapi.Depends(
        mlrun.api.api.deps.authenticate_request
    ),
    db_session: sqlalchemy.orm.Session = fastapi.Depends(
        mlrun.api.api.deps.get_db_session
    ),
):
    # Since there's no not-found option on get_project_background_task - we authorize before getting (unlike other
    # get endpoint)
    mlrun.api.utils.auth.verifier.AuthVerifier().query_project_resource_permissions(
        mlrun.api.schemas.AuthorizationResourceTypes.project_background_task,
        project,
        name,
        mlrun.api.schemas.AuthorizationAction.read,
        auth_info,
    )
    return mlrun.api.utils.background_tasks.ProjectBackgroundTasksHandler().get_background_task(
        db_session, name=name, project=project
    )


@router.get(
    "/background-tasks/{name}",
    response_model=mlrun.api.schemas.BackgroundTask,
)
def get_internal_background_task(
    name: str,
    request: fastapi.Request,
    auth_info: mlrun.api.schemas.AuthInfo = fastapi.Depends(
        mlrun.api.api.deps.authenticate_request
    ),
):
    # Since there's no not-found option on get_background_task - we authorize before getting (unlike other get endpoint)
    # In Iguazio 3.2 the manifest doesn't support the global background task resource - therefore we have to just omit
    # authorization
    # we also skip Iguazio 3.6 for now, until it will add support for it (still in development)
    igz_version = mlrun.mlconf.get_parsed_igz_version()
    if igz_version and igz_version >= semver.VersionInfo.parse("3.7.0-b1"):
        mlrun.api.utils.auth.verifier.AuthVerifier().query_resource_permissions(
            mlrun.api.schemas.AuthorizationResourceTypes.background_task,
            name,
            mlrun.api.schemas.AuthorizationAction.read,
            auth_info,
        )
    if (
        mlrun.mlconf.httpdb.clusterization.role
        == mlrun.api.schemas.ClusterizationRole.chief
    ):
        return mlrun.api.utils.background_tasks.InternalBackgroundTasksHandler().get_background_task(
            name=name
        )
    else:
        logger.info("Requesting internal background task, redirecting to chief")
        chief_client = mlrun.api.utils.clients.chief.Client()
        return chief_client.get_background_task(name=name, request=request)
