import deepdiff

import mlrun
import mlrun.errors


def test_mount_configmap():
    expected_volume = {"configMap": {"name": "my-config-map"}, "name": "my-volume"}
    expected_volume_mount = {"mountPath": "/myConfMapPath", "name": "my-volume"}

    function = mlrun.new_function(
        "function-name", "function-project", kind=mlrun.runtimes.RuntimeKinds.job
    )
    function.apply(
        mlrun.platforms.mount_configmap(
            configmap_name="my-config-map",
            mount_path="/myConfMapPath",
            volume_name="my-volume",
        )
    )

    assert (
        deepdiff.DeepDiff(
            [expected_volume],
            function.spec.volumes,
            ignore_order=True,
        )
        == {}
    )
    assert (
        deepdiff.DeepDiff(
            [expected_volume_mount],
            function.spec.volume_mounts,
            ignore_order=True,
        )
        == {}
    )


def test_mount_hostpath():
    expected_volume = {"hostPath": {"path": "/tmp", "type": ""}, "name": "my-volume"}
    expected_volume_mount = {"mountPath": "/myHostPath", "name": "my-volume"}

    function = mlrun.new_function(
        "function-name", "function-project", kind=mlrun.runtimes.RuntimeKinds.job
    )
    function.apply(
        mlrun.platforms.mount_hostpath(
            host_path="/tmp", mount_path="/myHostPath", volume_name="my-volume"
        )
    )

    assert (
        deepdiff.DeepDiff(
            [expected_volume],
            function.spec.volumes,
            ignore_order=True,
        )
        == {}
    )
    assert (
        deepdiff.DeepDiff(
            [expected_volume_mount],
            function.spec.volume_mounts,
            ignore_order=True,
        )
        == {}
    )


def test_mount_s3():
    function = mlrun.new_function(
        "function-name", "function-project", kind=mlrun.runtimes.RuntimeKinds.job
    )
    function.apply(
        mlrun.platforms.mount_s3(
            aws_access_key="xx", aws_secret_key="yy", endpoint_url="a.b"
        )
    )
    env_dict = {var["name"]: var["value"] for var in function.spec.env}
    assert env_dict == {
        "S3_ENDPOINT_URL": "a.b",
        "AWS_ACCESS_KEY_ID": "xx",
        "AWS_SECRET_ACCESS_KEY": "yy",
    }

    function = mlrun.new_function(
        "function-name", "function-project", kind=mlrun.runtimes.RuntimeKinds.job
    )
    function.apply(mlrun.platforms.mount_s3(secret_name="s", endpoint_url="a.b"))
    env_dict = {
        var["name"]: var.get("value", var.get("valueFrom")) for var in function.spec.env
    }
    assert env_dict == {
        "S3_ENDPOINT_URL": "a.b",
        "AWS_ACCESS_KEY_ID": {
            "secretKeyRef": {"key": "AWS_ACCESS_KEY_ID", "name": "s"}
        },
        "AWS_SECRET_ACCESS_KEY": {
            "secretKeyRef": {"key": "AWS_SECRET_ACCESS_KEY", "name": "s"}
        },
    }
