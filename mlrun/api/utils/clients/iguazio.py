import copy
import datetime
import enum
import http
import json
import typing
import urllib.parse

import fastapi
import requests.adapters
import urllib3

import mlrun.api.schemas
import mlrun.api.utils.projects.remotes.leader
import mlrun.errors
import mlrun.utils.helpers
import mlrun.utils.singleton
from mlrun.utils import logger


class JobStates:
    completed = "completed"
    failed = "failed"
    canceled = "canceled"
    in_progress = "in_progress"

    @staticmethod
    def all():
        return [
            JobStates.completed,
            JobStates.failed,
            JobStates.canceled,
            JobStates.in_progress,
        ]

    @staticmethod
    def terminal_states():
        return [
            JobStates.completed,
            JobStates.failed,
            JobStates.canceled,
        ]


class SessionPlanes:
    data = "data"
    control = "control"

    @staticmethod
    def all():
        return [
            SessionPlanes.data,
            SessionPlanes.control,
        ]


class Client(
    mlrun.api.utils.projects.remotes.leader.Member,
    metaclass=mlrun.utils.singleton.AbstractSingleton,
):
    def __init__(self) -> None:
        super().__init__()
        http_adapter = requests.adapters.HTTPAdapter(
            max_retries=urllib3.util.retry.Retry(total=3, backoff_factor=1),
            pool_maxsize=int(mlrun.mlconf.httpdb.max_workers),
        )
        self._session = requests.Session()
        self._session.mount("http://", http_adapter)
        self._api_url = mlrun.mlconf.iguazio_api_url
        # The job is expected to be completed in less than 5 seconds. If 10 seconds have passed and the job
        # has not been completed, increase the interval to retry every 5 seconds
        self._wait_for_job_completion_retry_interval = mlrun.utils.create_step_backoff(
            [[1, 10], [5, None]]
        )
        self._wait_for_project_terminal_state_retry_interval = 5

    def try_get_grafana_service_url(self, session: str) -> typing.Optional[str]:
        """
        Try to find a ready grafana app service, and return its URL
        If nothing found, returns None
        """
        logger.debug("Getting grafana service url from Iguazio")
        response = self._send_request_to_api(
            "GET",
            "app_services_manifests",
            "Failed getting app services manifests from Iguazio",
            session,
        )
        response_body = response.json()
        for app_services_manifest in response_body.get("data", []):
            for app_service in app_services_manifest.get("attributes", {}).get(
                "app_services", []
            ):
                if (
                    app_service.get("spec", {}).get("kind") == "grafana"
                    and app_service.get("status", {}).get("state") == "ready"
                    and len(app_service.get("status", {}).get("urls", [])) > 0
                ):
                    url_kind_to_url = {}
                    for url in app_service["status"]["urls"]:
                        url_kind_to_url[url["kind"]] = url["url"]
                    # precedence for https
                    for kind in ["https", "http"]:
                        if kind in url_kind_to_url:
                            return url_kind_to_url[kind]
        return None

    def verify_request_session(
        self, request: fastapi.Request
    ) -> mlrun.api.schemas.AuthInfo:
        """
        Proxy the request to one of the session verification endpoints (which will verify the session of the request)
        """
        response = self._send_request_to_api(
            "POST",
            mlrun.mlconf.httpdb.authentication.iguazio.session_verification_endpoint,
            "Failed verifying iguazio session",
            headers={
                "authorization": request.headers.get("authorization"),
                "cookie": request.headers.get("cookie"),
            },
        )
        return self._generate_auth_info_from_session_verification_response(response)

    def verify_session(self, session: str) -> mlrun.api.schemas.AuthInfo:
        response = self._send_request_to_api(
            "POST",
            mlrun.mlconf.httpdb.authentication.iguazio.session_verification_endpoint,
            "Failed verifying iguazio session",
            session,
        )
        return self._generate_auth_info_from_session_verification_response(response)

    def get_or_create_access_key(
        self, session: str, planes: typing.List[str] = None
    ) -> str:
        if planes is None:
            planes = [
                SessionPlanes.data,
                SessionPlanes.control,
            ]
        body = {
            "data": {
                "type": "access_key",
                "attributes": {"label": "MLRun", "planes": planes},
            }
        }
        response = self._send_request_to_api(
            "POST",
            "self/get_or_create_access_key",
            "Failed getting or creating iguazio access key",
            session,
            json=body,
        )
        if response.status_code == http.HTTPStatus.CREATED.value:
            logger.debug("Created access key in Iguazio", planes=planes)
        return response.json()["data"]["id"]

    def create_project(
        self,
        session: str,
        project: mlrun.api.schemas.Project,
        wait_for_completion: bool = True,
    ) -> bool:
        logger.debug("Creating project in Iguazio", project=project)
        body = self._transform_mlrun_project_to_iguazio_project(project)
        return self._create_project_in_iguazio(session, body, wait_for_completion)

    def update_project(
        self,
        session: str,
        name: str,
        project: mlrun.api.schemas.Project,
    ):
        logger.debug("Updating project in Iguazio", name=name, project=project)
        body = self._transform_mlrun_project_to_iguazio_project(project)
        self._put_project_to_iguazio(session, name, body)

    def delete_project(
        self,
        session: str,
        name: str,
        deletion_strategy: mlrun.api.schemas.DeletionStrategy = mlrun.api.schemas.DeletionStrategy.default(),
        wait_for_completion: bool = True,
    ) -> bool:
        logger.debug(
            "Deleting project in Iguazio",
            name=name,
            deletion_strategy=deletion_strategy,
        )
        body = self._transform_mlrun_project_to_iguazio_project(
            mlrun.api.schemas.Project(
                metadata=mlrun.api.schemas.ProjectMetadata(name=name)
            )
        )
        headers = {
            "igz-project-deletion-strategy": deletion_strategy.to_iguazio_deletion_strategy(),
        }
        try:
            response = self._send_request_to_api(
                "DELETE",
                "projects",
                "Failed deleting project in Iguazio",
                session,
                headers=headers,
                json=body,
            )
        except requests.HTTPError as exc:
            if exc.response.status_code != http.HTTPStatus.NOT_FOUND.value:
                raise
            logger.debug(
                "Project not found in Iguazio. Considering deletion as successful",
                name=name,
                deletion_strategy=deletion_strategy,
            )
            return False
        else:
            if wait_for_completion:
                job_id = response.json()["data"]["id"]
                self._wait_for_job_completion(
                    session, job_id, "Project deletion job failed"
                )
                return False
            return True

    def list_projects(
        self,
        session: str,
        updated_after: typing.Optional[datetime.datetime] = None,
        page_size: typing.Optional[int] = None,
    ) -> typing.Tuple[
        typing.List[mlrun.api.schemas.Project], typing.Optional[datetime.datetime]
    ]:
        params = {}
        if updated_after is not None:
            time_string = updated_after.isoformat().split("+")[0]
            params = {"filter[updated_at]": f"[$gt]{time_string}Z"}
        if page_size is None:
            page_size = (
                mlrun.mlconf.httpdb.projects.iguazio_list_projects_default_page_size
            )
        if page_size is not None:
            params["page[size]"] = int(page_size)

        params["include"] = "owner"
        response = self._send_request_to_api(
            "GET",
            "projects",
            "Failed listing projects from Iguazio",
            session,
            params=params,
        )
        response_body = response.json()
        projects = []
        for iguazio_project in response_body["data"]:
            projects.append(
                self._transform_iguazio_project_to_mlrun_project(iguazio_project)
            )
        latest_updated_at = self._find_latest_updated_at(response_body)
        return projects, latest_updated_at

    def get_project(
        self,
        session: str,
        name: str,
    ) -> mlrun.api.schemas.Project:
        return self._get_project_from_iguazio(session, name)

    def get_project_owner(
        self,
        session: str,
        name: str,
    ) -> mlrun.api.schemas.ProjectOwner:
        response = self._get_project_from_iguazio_without_parsing(
            session, name, include_owner_session=True
        )
        iguazio_project = response.json()["data"]
        return mlrun.api.schemas.ProjectOwner(
            username=iguazio_project["attributes"]["owner_username"],
            session=iguazio_project["attributes"]["owner_access_key"],
        )

    def format_as_leader_project(
        self, project: mlrun.api.schemas.Project
    ) -> mlrun.api.schemas.IguazioProject:
        return mlrun.api.schemas.IguazioProject(
            data=self._transform_mlrun_project_to_iguazio_project(project)["data"]
        )

    def _find_latest_updated_at(
        self, response_body: dict
    ) -> typing.Optional[datetime.datetime]:
        latest_updated_at = None
        for iguazio_project in response_body["data"]:
            updated_at = datetime.datetime.fromisoformat(
                iguazio_project["attributes"]["updated_at"]
            )
            if latest_updated_at is None or latest_updated_at < updated_at:
                latest_updated_at = updated_at
        return latest_updated_at

    def _create_project_in_iguazio(
        self, session: str, body: dict, wait_for_completion: bool
    ) -> bool:
        _, job_id = self._post_project_to_iguazio(session, body)
        if wait_for_completion:
            self._wait_for_job_completion(
                session, job_id, "Project creation job failed"
            )
            return False
        return True

    def _post_project_to_iguazio(
        self, session: str, body: dict
    ) -> typing.Tuple[mlrun.api.schemas.Project, str]:
        response = self._send_request_to_api(
            "POST", "projects", "Failed creating project in Iguazio", session, json=body
        )
        response_body = response.json()
        return (
            self._transform_iguazio_project_to_mlrun_project(response_body["data"]),
            response_body["data"]["relationships"]["last_job"]["data"]["id"],
        )

    def _put_project_to_iguazio(
        self, session: str, name: str, body: dict
    ) -> mlrun.api.schemas.Project:
        response = self._send_request_to_api(
            "PUT",
            f"projects/__name__/{name}",
            "Failed updating project in Iguazio",
            session,
            json=body,
        )
        return self._transform_iguazio_project_to_mlrun_project(response.json()["data"])

    def _get_project_from_iguazio_without_parsing(
        self, session: str, name: str, include_owner_session: bool = False
    ):
        params = {"include": "owner"}
        if include_owner_session:
            params["enrich_owner_access_key"] = "true"
        return self._send_request_to_api(
            "GET",
            f"projects/__name__/{name}",
            "Failed getting project from Iguazio",
            session,
            params=params,
        )

    def _get_project_from_iguazio(
        self, session: str, name: str, include_owner_session: bool = False
    ) -> mlrun.api.schemas.Project:
        response = self._get_project_from_iguazio_without_parsing(session, name)
        return self._transform_iguazio_project_to_mlrun_project(response.json()["data"])

    def _wait_for_job_completion(self, session: str, job_id: str, error_message: str):
        def _verify_job_in_terminal_state():
            response = self._send_request_to_api(
                "GET", f"jobs/{job_id}", "Failed getting job from Iguazio", session
            )
            response_body = response.json()
            _job_state = response_body["data"]["attributes"]["state"]
            if _job_state not in JobStates.terminal_states():
                raise Exception(f"Job in progress. State: {_job_state}")
            return _job_state, response_body["data"]["attributes"]["result"]

        job_state, job_result = mlrun.utils.helpers.retry_until_successful(
            self._wait_for_job_completion_retry_interval,
            360,
            logger,
            False,
            _verify_job_in_terminal_state,
        )
        if job_state != JobStates.completed:
            status_code = None
            try:
                parsed_result = json.loads(job_result)
                error_message = f"{error_message} {parsed_result['message']}"
                # status is optional
                if "status" in parsed_result:
                    status_code = int(parsed_result["status"])
            except Exception:
                pass
            if not status_code:
                raise mlrun.errors.MLRunRuntimeError(error_message)
            raise mlrun.errors.raise_for_status_code(status_code, error_message)

    def _send_request_to_api(
        self, method, path, error_message: str, session=None, **kwargs
    ):
        url = f"{self._api_url}/api/{path}"
        # support session being already a cookie
        session_cookie = session
        if (
            session_cookie
            and not session_cookie.startswith('j:{"sid"')
            and not session_cookie.startswith(urllib.parse.quote_plus('j:{"sid"'))
        ):
            session_cookie = f'j:{{"sid": "{session_cookie}"}}'
        if session_cookie:
            cookies = kwargs.get("cookies", {})
            # in case some dev using this function for some reason setting cookies manually through kwargs + have a
            # cookie with "session" key there + filling the session cookie - explode
            if "session" in cookies and cookies["session"] != session_cookie:
                raise mlrun.errors.MLRunInvalidArgumentError(
                    "Session cookie already set"
                )
            cookies["session"] = session_cookie
            kwargs["cookies"] = cookies
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = 20
        if "projects" in path:
            if mlrun.api.schemas.HeaderNames.projects_role not in kwargs.get(
                "headers", {}
            ):
                kwargs.setdefault("headers", {})[
                    mlrun.api.schemas.HeaderNames.projects_role
                ] = "mlrun"

        # requests no longer supports header values to be enum (https://github.com/psf/requests/pull/6154)
        # convert to strings. Do the same for params for niceness
        for kwarg in ["headers", "params"]:
            dict_ = kwargs.get(kwarg, {})
            for key in dict_.keys():
                if isinstance(dict_[key], enum.Enum):
                    dict_[key] = dict_[key].value
        response = self._session.request(method, url, verify=False, **kwargs)
        if not response.ok:
            log_kwargs = copy.deepcopy(kwargs)
            log_kwargs.update({"method": method, "path": path})
            if response.content:
                try:
                    data = response.json()
                    ctx = data.get("meta", {}).get("ctx")
                    errors = data.get("errors", [])
                except Exception:
                    pass
                else:
                    error_message = f"{error_message}: {str(errors)}"
                    log_kwargs.update({"ctx": ctx, "errors": errors})
            logger.warning("Request to iguazio failed", **log_kwargs)
            mlrun.errors.raise_for_status(response, error_message)
        return response

    @staticmethod
    def _generate_auth_info_from_session_verification_response(
        response: requests.Response,
    ) -> mlrun.api.schemas.AuthInfo:
        gids = response.headers.get("x-user-group-ids")
        if gids:
            gids = gids.split(",")
        planes = response.headers.get("x-v3io-session-planes")
        if planes:
            planes = planes.split(",")
        planes = planes or []
        auth_info = mlrun.api.schemas.AuthInfo(
            username=response.headers["x-remote-user"],
            session=response.headers["x-v3io-session-key"],
            user_id=response.headers.get("x-user-id"),
            user_group_ids=gids or [],
        )
        if SessionPlanes.data in planes:
            auth_info.data_session = auth_info.session
        return auth_info

    @staticmethod
    def _transform_mlrun_project_to_iguazio_project(
        project: mlrun.api.schemas.Project,
    ) -> dict:
        body = {
            "data": {
                "type": "project",
                "attributes": {
                    "name": project.metadata.name,
                    "description": project.spec.description,
                    "admin_status": project.spec.desired_state,
                    "mlrun_project": Client._transform_mlrun_project_to_iguazio_mlrun_project_attribute(
                        project
                    ),
                },
            }
        }
        if project.metadata.created:
            body["data"]["attributes"][
                "created_at"
            ] = project.metadata.created.isoformat()
        if project.metadata.labels:
            body["data"]["attributes"][
                "labels"
            ] = Client._transform_mlrun_labels_to_iguazio_labels(
                project.metadata.labels
            )
        if project.metadata.annotations:
            body["data"]["attributes"][
                "annotations"
            ] = Client._transform_mlrun_labels_to_iguazio_labels(
                project.metadata.annotations
            )
        if project.spec.owner:
            body["data"]["attributes"]["owner_username"] = project.spec.owner
        return body

    @staticmethod
    def _transform_mlrun_project_to_iguazio_mlrun_project_attribute(
        project: mlrun.api.schemas.Project,
    ):
        project_dict = project.dict(
            exclude_unset=True,
            exclude={
                "metadata": {"name", "created", "labels", "annotations"},
                "spec": {"description", "desired_state", "owner"},
                "status": {"state"},
            },
        )
        # ensure basic fields exist (schema should take care of that but we exclude, so status might be missing for
        # example)
        for field in ["metadata", "spec", "status"]:
            project_dict.setdefault(field, {})
        return json.dumps(project_dict)

    @staticmethod
    def _transform_mlrun_labels_to_iguazio_labels(
        mlrun_labels: dict,
    ) -> typing.List[dict]:
        iguazio_labels = []
        for label_key, label_value in mlrun_labels.items():
            iguazio_labels.append({"name": label_key, "value": label_value})
        return iguazio_labels

    @staticmethod
    def _transform_iguazio_labels_to_mlrun_labels(
        iguazio_labels: typing.List[dict],
    ) -> dict:
        return {label["name"]: label["value"] for label in iguazio_labels}

    @staticmethod
    def _transform_iguazio_project_to_mlrun_project(
        iguazio_project,
    ) -> mlrun.api.schemas.Project:
        mlrun_project_without_common_fields = json.loads(
            iguazio_project["attributes"].get("mlrun_project", "{}")
        )
        # name is mandatory in the mlrun schema, without adding it the schema initialization will fail
        mlrun_project_without_common_fields.setdefault("metadata", {})[
            "name"
        ] = iguazio_project["attributes"]["name"]
        mlrun_project = mlrun.api.schemas.Project(**mlrun_project_without_common_fields)
        mlrun_project.metadata.created = datetime.datetime.fromisoformat(
            iguazio_project["attributes"]["created_at"]
        )
        mlrun_project.spec.desired_state = mlrun.api.schemas.ProjectDesiredState(
            iguazio_project["attributes"]["admin_status"]
        )
        mlrun_project.status.state = mlrun.api.schemas.ProjectState(
            iguazio_project["attributes"]["operational_status"]
        )
        if iguazio_project["attributes"].get("description"):
            mlrun_project.spec.description = iguazio_project["attributes"][
                "description"
            ]
        if iguazio_project["attributes"].get("labels"):
            mlrun_project.metadata.labels = (
                Client._transform_iguazio_labels_to_mlrun_labels(
                    iguazio_project["attributes"]["labels"]
                )
            )
        if iguazio_project["attributes"].get("annotations"):
            mlrun_project.metadata.annotations = (
                Client._transform_iguazio_labels_to_mlrun_labels(
                    iguazio_project["attributes"]["annotations"]
                )
            )
        if iguazio_project["attributes"].get("owner_username"):
            mlrun_project.spec.owner = iguazio_project["attributes"]["owner_username"]
        return mlrun_project
