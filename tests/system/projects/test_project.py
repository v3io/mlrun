import pathlib
import re
import shutil
import sys
import traceback
from subprocess import PIPE, run
from sys import executable, stderr

import pytest
from kfp import dsl

import mlrun
from mlrun.artifacts import Artifact
from mlrun.model import EntrypointParam
from mlrun.utils import logger
from tests.conftest import out_path
from tests.system.base import TestMLRunSystem

data_url = "https://s3.wasabisys.com/iguazio/data/iris/iris.data.raw.csv"
model_pkg_class = "sklearn.linear_model.LogisticRegression"
projects_dir = f"{out_path}/proj"
funcs = mlrun.projects.pipeline_context.functions


def exec_project(args, cwd=None):
    cmd = [executable, "-m", "mlrun", "project"] + args
    out = run(cmd, stdout=PIPE, stderr=PIPE, cwd=cwd)
    if out.returncode != 0:
        print(out.stderr.decode("utf-8"), file=stderr)
        print(out.stdout.decode("utf-8"), file=stderr)
        print(traceback.format_exc())
        raise Exception(out.stderr.decode("utf-8"))
    return out.stdout.decode("utf-8")


# pipeline for inline test (run pipeline from handler)
@dsl.pipeline(name="test pipeline", description="test")
def pipe_test():
    # train the model using a library (hub://) function and the generated data
    funcs["train"].as_step(
        name="train",
        inputs={"dataset": data_url},
        params={"model_pkg_class": model_pkg_class, "label_column": "label"},
        outputs=["model", "test_set"],
    )


# Marked as enterprise because of v3io mount and pipelines
@TestMLRunSystem.skip_test_if_env_not_configured
@pytest.mark.enterprise
class TestProject(TestMLRunSystem):
    project_name = "project-system-test-project"

    def custom_setup(self):
        pass

    @property
    def assets_path(self):
        return (
            pathlib.Path(sys.modules[self.__module__].__file__).absolute().parent
            / "assets"
        )

    def _create_project(self, project_name, with_repo=False):
        proj = mlrun.new_project(project_name, str(self.assets_path))
        proj.set_function(
            "prep_data.py",
            "prep-data",
            image="mlrun/mlrun",
            handler="prep_data",
            with_repo=with_repo,
        )
        proj.set_function("hub://describe")
        proj.set_function("hub://sklearn_classifier", "train")
        proj.set_function("hub://test_classifier", "test")
        proj.set_function("hub://v2_model_server", "serving")
        proj.set_artifact("data", Artifact(target_path=data_url))
        proj.spec.params = {"label_column": "label"}
        arg = EntrypointParam(
            "model_pkg_class",
            type="str",
            default=model_pkg_class,
            doc="model package/algorithm",
        )
        proj.set_workflow("main", "./kflow.py", args_schema=[arg])
        proj.set_workflow("newflow", "./newflow.py", handler="newpipe")
        proj.spec.artifact_path = "v3io:///projects/{{run.project}}"
        proj.save()
        return proj

    def test_run(self):
        name = "pipe1"
        # create project in context
        self._create_project(name)

        # load project from context dir and run a workflow
        project2 = mlrun.load_project(str(self.assets_path), name=name)
        run = project2.run("main", watch=True, artifact_path=f"v3io:///projects/{name}")
        assert run.state == mlrun.run.RunStatuses.succeeded, "pipeline failed"

        # test the list_runs/artifacts/functions methods
        runs_list = project2.list_runs(name="test", labels={"workflow": run.run_id})
        runs = runs_list.to_objects()
        assert runs[0].status.state == "completed"
        assert runs[0].metadata.name == "test"
        runs_list.compare(filename=f"{projects_dir}/compare.html")
        artifacts = project2.list_artifacts(tag=run.run_id).to_objects()
        assert len(artifacts) == 4  # cleaned_data, test_set_preds, model, test_set
        assert artifacts[0].producer["workflow"] == run.run_id

        models = project2.list_models(tag=run.run_id)
        assert len(models) == 1
        assert models[0].producer["workflow"] == run.run_id

        functions = project2.list_functions(tag="latest")
        assert len(functions) == 3  # prep-data, train, test
        assert functions[0].metadata.project == name

        self._delete_test_project(name)

    def test_run_artifact_path(self):
        name = "pipe1"
        # create project in context
        self._create_project(name)

        # load project from context dir and run a workflow
        project = mlrun.load_project(str(self.assets_path), name=name)
        # Don't provide an artifact-path, to verify that the run-id is added by default
        workflow_run = project.run("main", watch=True)
        assert workflow_run.state == mlrun.run.RunStatuses.succeeded, "pipeline failed"

        # check that the functions running in the workflow had the output_path set correctly
        db = mlrun.get_run_db()
        run_id = workflow_run.run_id
        pipeline = db.get_pipeline(run_id, project=name)
        for graph_step in pipeline["graph"].values():
            if "run_uid" in graph_step:
                run_object = db.read_run(uid=graph_step["run_uid"], project=name)
                output_path = run_object["spec"]["output_path"]
                assert output_path == f"v3io:///projects/{name}/{run_id}"
        self._delete_test_project(name)

    def test_run_git_load(self):
        # load project from git
        name = "pipe2"
        project_dir = f"{projects_dir}/{name}"
        shutil.rmtree(project_dir, ignore_errors=True)

        project2 = mlrun.load_project(
            project_dir, "git://github.com/mlrun/project-demo.git#main", name=name
        )
        logger.info("run pipeline from git")

        # run project, load source into container at runtime
        project2.spec.load_source_on_run = True
        run = project2.run("main", artifact_path=f"v3io:///projects/{name}")
        run.wait_for_completion()
        assert run.state == mlrun.run.RunStatuses.succeeded, "pipeline failed"
        self._delete_test_project(name)

    def test_run_git_build(self):
        name = "pipe3"
        project_dir = f"{projects_dir}/{name}"
        shutil.rmtree(project_dir, ignore_errors=True)

        # load project from git, build the container image from source (in the workflow)
        project2 = mlrun.load_project(
            project_dir, "git://github.com/mlrun/project-demo.git#main", name=name
        )
        logger.info("run pipeline from git")
        project2.spec.load_source_on_run = False
        run = project2.run(
            "main",
            artifact_path=f"v3io:///projects/{name}",
            arguments={"build": 1},
            workflow_path=str(self.assets_path / "kflow.py"),
        )
        run.wait_for_completion()
        assert run.state == mlrun.run.RunStatuses.succeeded, "pipeline failed"
        self._delete_test_project(name)

    def test_run_cli(self):
        # load project from git
        name = "pipe4"
        project_dir = f"{projects_dir}/{name}"
        shutil.rmtree(project_dir, ignore_errors=True)

        # clone a project to local dir
        args = [
            "-n",
            name,
            "-u",
            "git://github.com/mlrun/project-demo.git",
            project_dir,
        ]
        out = exec_project(args, projects_dir)
        print(out)

        # load the project from local dir and change a workflow
        project2 = mlrun.load_project(project_dir)
        project2.spec.workflows = {}
        project2.set_workflow("kf", "./kflow.py")
        project2.save()
        print(project2.to_yaml())

        # exec the workflow
        args = [
            "-n",
            name,
            "-r",
            "kf",
            "-w",
            "-p",
            f"v3io:///projects/{name}",
            project_dir,
        ]
        out = exec_project(args, projects_dir)
        m = re.search(" Pipeline run id=(.+),", out)
        assert m, "pipeline id is not in output"

        run_id = m.group(1).strip()
        db = mlrun.get_run_db()
        pipeline = db.get_pipeline(run_id, project=name)
        state = pipeline["run"]["status"]
        assert state == mlrun.run.RunStatuses.succeeded, "pipeline failed"
        self._delete_test_project(name)
        self._delete_test_project(project2.metadata.name)

    def test_inline_pipeline(self):
        name = "pipe5"
        project_dir = f"{projects_dir}/{name}"
        shutil.rmtree(project_dir, ignore_errors=True)
        project = self._create_project(name, True)
        run = project.run(
            artifact_path=f"v3io:///projects/{name}/artifacts",
            workflow_handler=pipe_test,
        )
        run.wait_for_completion()
        assert run.state == mlrun.run.RunStatuses.succeeded, "pipeline failed"
        self._delete_test_project(name)

    def test_get_or_create(self):
        # create project and save to DB
        name = "newproj73"
        project_dir = f"{projects_dir}/{name}"
        shutil.rmtree(project_dir, ignore_errors=True)
        project = mlrun.get_or_create_project(name, project_dir)
        project.spec.description = "mytest"
        project.save()

        # get project should read from DB
        shutil.rmtree(project_dir, ignore_errors=True)
        project = mlrun.get_or_create_project(name, project_dir)
        project.save()
        assert project.spec.description == "mytest", "failed to get project"
        self._delete_test_project(name)

        # get project should read from context (project.yaml)
        project = mlrun.get_or_create_project(name, project_dir)
        assert project.spec.description == "mytest", "failed to get project"
        self._delete_test_project(name)

    def _test_new_pipeline(self, name, engine):
        project = self._create_project(name)
        project.set_function(
            "gen_iris.py",
            "gen-iris",
            image="mlrun/mlrun",
            handler="iris_generator",
            requirements=["requests"],
        )
        print(project.to_yaml())
        run = project.run(
            "newflow",
            engine=engine,
            artifact_path=f"v3io:///projects/{name}",
            watch=True,
        )
        assert run.state == mlrun.run.RunStatuses.succeeded, "pipeline failed"
        fn = project.get_function("gen-iris", ignore_cache=True)
        assert fn.status.state == "ready"
        assert fn.spec.image, "image path got cleared"
        self._delete_test_project(name)

    def test_local_pipeline(self):
        self._test_new_pipeline("lclpipe", engine="local")

    def test_kfp_pipeline(self):
        self._test_new_pipeline("kfppipe", engine="kfp")

    def test_local_cli(self):
        # load project from git
        name = "lclclipipe"
        project = self._create_project(name)
        project.set_function(
            "gen_iris.py",
            "gen-iris",
            image="mlrun/mlrun",
            handler="iris_generator",
        )
        project.save()
        print(project.to_yaml())

        # exec the workflow
        args = [
            "-n",
            name,
            "-r",
            "newflow",
            "--engine",
            "local",
            "-w",
            "-p",
            f"v3io:///projects/{name}",
            str(self.assets_path),
        ]
        out = exec_project(args, projects_dir)
        print("OUT:\n", out)
        assert out.find("pipeline run finished, state=Succeeded"), "pipeline failed"
        self._delete_test_project(name)

    def test_build_and_run(self):
        # test that build creates a proper image and run will use the updated function (with the built image)
        name = "buildandrun"
        project = mlrun.new_project(name, context=str(self.assets_path))

        # test with user provided function object
        base_fn = mlrun.code_to_function(
            "scores",
            filename=str(self.assets_path / "sentiment.py"),
            kind="job",
            image="mlrun/mlrun",
            requirements=["vaderSentiment"],
            handler="handler",
        )

        fn = base_fn.copy()
        assert fn.spec.build.base_image == "mlrun/mlrun" and not fn.spec.image
        fn.spec.build.with_mlrun = False
        project.build_function(fn)
        run_result = project.run_function(fn, params={"text": "good morning"})
        assert run_result.output("score")

        # test with function from project spec
        project.set_function(
            "./sentiment.py",
            "scores2",
            kind="job",
            image="mlrun/mlrun",
            requirements=["vaderSentiment"],
            handler="handler",
        )
        project.build_function("scores2")
        run_result = project.run_function("scores2", params={"text": "good morning"})
        assert run_result.output("score")

        # test auto build option (the function will be built on the first time, then run)
        fn = base_fn.copy()
        fn.metadata.name = "scores3"
        fn.spec.build.auto_build = True
        run_result = project.run_function(fn, params={"text": "good morning"})
        assert fn.status.state == "ready"
        assert fn.spec.image, "image path got cleared"
        assert run_result.output("score")

        self._delete_test_project(name)

    def test_set_secrets(self):
        name = "set-secrets"
        project = mlrun.new_project(name, context=str(self.assets_path))
        project.save()
        env_file = str(self.assets_path / "envfile")
        db = mlrun.get_run_db()
        db.delete_project_secrets(name, provider="kubernetes")
        project.set_secrets(file_path=env_file)
        secrets = db.list_project_secret_keys(name, provider="kubernetes")
        assert secrets.secret_keys == ["ENV_ARG1", "ENV_ARG2"]

        # Cleanup
        self._run_db.delete_project_secrets(self.project_name, provider="kubernetes")
        self._delete_test_project(name)
