import os
import platform
import sys
import time

import numpy as np
import pytest

import ray
import ray._private.gcs_utils as gcs_utils
import ray.experimental.internal_kv as internal_kv
from ray._private.test_utils import (
    make_global_state_accessor,
    get_metric_check_condition,
    MetricSamplePattern,
)
from ray.util.placement_group import placement_group
from ray.util.scheduling_strategies import (
    NodeAffinitySchedulingStrategy,
    PlacementGroupSchedulingStrategy,
)
from ray._common.test_utils import SignalActor, wait_for_condition
from ray.util.state import list_tasks


@pytest.mark.skipif(
    platform.system() == "Windows", reason="Failing on Windows. Multi node."
)
def test_load_balancing_under_constrained_memory(
    enable_mac_large_object_store, ray_start_cluster
):
    # This test ensures that tasks are being assigned to all raylets in a
    # roughly equal manner even when the tasks have dependencies.
    cluster = ray_start_cluster
    num_nodes = 3
    num_cpus = 4
    object_size = 4e7
    num_tasks = 100
    for _ in range(num_nodes):
        cluster.add_node(
            num_cpus=num_cpus,
            memory=(num_cpus - 2) * object_size,
            object_store_memory=(num_cpus - 2) * object_size,
        )
    cluster.add_node(
        num_cpus=0,
        resources={"custom": 1},
        memory=(num_tasks + 1) * object_size,
        object_store_memory=(num_tasks + 1) * object_size,
    )
    ray.init(address=cluster.address)

    @ray.remote(num_cpus=0, resources={"custom": 1})
    def create_object():
        return np.zeros(int(object_size), dtype=np.uint8)

    @ray.remote
    def f(i, x):
        print(i, ray._private.worker.global_worker.node.unique_id)
        time.sleep(0.1)
        return ray._private.worker.global_worker.node.unique_id

    deps = [create_object.remote() for _ in range(num_tasks)]
    for i, dep in enumerate(deps):
        print(i, dep)

    # TODO(swang): Actually test load balancing. Load balancing is currently
    # flaky on Travis, probably due to the scheduling policy ping-ponging
    # waiting tasks.
    deps = [create_object.remote() for _ in range(num_tasks)]
    tasks = [f.remote(i, dep) for i, dep in enumerate(deps)]
    for i, dep in enumerate(deps):
        print(i, dep)
    ray.get(tasks)


def test_critical_object_store_mem_resource_utilization(ray_start_cluster):
    cluster = ray_start_cluster
    cluster.add_node(
        _system_config={
            "scheduler_spread_threshold": 0.0,
        },
    )
    ray.init(address=cluster.address)
    non_local_node = cluster.add_node()
    cluster.wait_for_nodes()

    x = ray.put(np.zeros(1024 * 1024, dtype=np.uint8))
    print(x)

    @ray.remote
    def f():
        return ray._private.worker.global_worker.node.unique_id

    # Wait for resource availabilities to propagate.
    time.sleep(1)
    # The task should be scheduled to the remote node since
    # local node has non-zero object store mem utilization.
    assert ray.get(f.remote()) == non_local_node.unique_id


def test_default_scheduling_strategy(ray_start_cluster):
    cluster = ray_start_cluster
    cluster.add_node(
        num_cpus=16,
        resources={"head": 1},
        _system_config={"scheduler_spread_threshold": 1},
    )
    cluster.add_node(num_cpus=8, num_gpus=8, resources={"worker": 1})
    cluster.wait_for_nodes()

    ray.init(address=cluster.address)
    pg = ray.util.placement_group(bundles=[{"CPU": 1, "GPU": 1}, {"CPU": 1, "GPU": 1}])
    ray.get(pg.ready())
    ray.get(pg.ready())

    @ray.remote(scheduling_strategy="DEFAULT")
    def get_node_id_1():
        return ray._private.worker.global_worker.current_node_id

    head_node_id = ray.get(get_node_id_1.options(resources={"head": 1}).remote())
    worker_node_id = ray.get(get_node_id_1.options(resources={"worker": 1}).remote())

    assert ray.get(get_node_id_1.remote()) == head_node_id

    @ray.remote(
        num_cpus=1,
        scheduling_strategy=PlacementGroupSchedulingStrategy(placement_group=pg),
    )
    def get_node_id_2():
        return ray._private.worker.global_worker.current_node_id

    assert (
        ray.get(get_node_id_2.options(scheduling_strategy="DEFAULT").remote())
        == head_node_id
    )

    @ray.remote
    def get_node_id_3():
        return ray._private.worker.global_worker.current_node_id

    @ray.remote(
        num_cpus=1,
        scheduling_strategy=PlacementGroupSchedulingStrategy(
            placement_group=pg, placement_group_capture_child_tasks=True
        ),
    )
    class Actor1:
        def get_node_ids(self):
            return [
                ray._private.worker.global_worker.current_node_id,
                # Use parent's placement group
                ray.get(get_node_id_3.remote()),
                ray.get(get_node_id_3.options(scheduling_strategy="DEFAULT").remote()),
            ]

    actor1 = Actor1.remote()
    assert ray.get(actor1.get_node_ids.remote()) == [
        worker_node_id,
        worker_node_id,
        head_node_id,
    ]


@pytest.mark.skipif(
    ray._private.client_mode_hook.is_client_mode_enabled, reason="Fails w/ Ray Client."
)
def test_placement_group_scheduling_strategy(ray_start_cluster):
    cluster = ray_start_cluster
    cluster.add_node(num_cpus=8, resources={"head": 1})
    cluster.add_node(num_cpus=8, num_gpus=8, resources={"worker": 1})
    cluster.wait_for_nodes()

    ray.init(address=cluster.address)
    pg = ray.util.placement_group(bundles=[{"CPU": 1, "GPU": 1}, {"CPU": 1, "GPU": 1}])
    ray.get(pg.ready())

    @ray.remote(scheduling_strategy="DEFAULT")
    def get_node_id_1():
        return ray._private.worker.global_worker.current_node_id

    worker_node_id = ray.get(get_node_id_1.options(resources={"worker": 1}).remote())

    assert (
        ray.get(
            get_node_id_1.options(
                num_cpus=1,
                scheduling_strategy=PlacementGroupSchedulingStrategy(
                    placement_group=pg
                ),
            ).remote()
        )
        == worker_node_id
    )

    @ray.remote(
        num_cpus=1,
        scheduling_strategy=PlacementGroupSchedulingStrategy(placement_group=pg),
    )
    def get_node_id_2():
        return ray._private.worker.global_worker.current_node_id

    assert ray.get(get_node_id_2.remote()) == worker_node_id

    @ray.remote(
        num_cpus=1,
        scheduling_strategy=PlacementGroupSchedulingStrategy(placement_group=pg),
    )
    class Actor1:
        def get_node_id(self):
            return ray._private.worker.global_worker.current_node_id

    actor1 = Actor1.remote()
    assert ray.get(actor1.get_node_id.remote()) == worker_node_id

    @ray.remote
    class Actor2:
        def get_node_id(self):
            return ray._private.worker.global_worker.current_node_id

    actor2 = Actor2.options(
        scheduling_strategy=PlacementGroupSchedulingStrategy(placement_group=pg)
    ).remote()
    assert ray.get(actor2.get_node_id.remote()) == worker_node_id

    with pytest.raises(ValueError):

        @ray.remote(
            scheduling_strategy=PlacementGroupSchedulingStrategy(placement_group=pg)
        )
        def func():
            return 0

        func.options(placement_group=pg).remote()

    with pytest.raises(ValueError):

        @ray.remote
        def func():
            return 0

        func.options(scheduling_strategy="XXX").remote()


def test_node_affinity_scheduling_strategy(monkeypatch, ray_start_cluster):
    cluster = ray_start_cluster
    cluster.add_node(num_cpus=8, resources={"head": 1})
    ray.init(address=cluster.address)
    cluster.add_node(num_cpus=8, resources={"worker": 1})
    cluster.wait_for_nodes()

    @ray.remote
    def get_node_id():
        return ray.get_runtime_context().get_node_id()

    head_node_id = ray.get(
        get_node_id.options(num_cpus=0, resources={"head": 1}).remote()
    )
    worker_node_id = ray.get(
        get_node_id.options(num_cpus=0, resources={"worker": 1}).remote()
    )

    assert worker_node_id == ray.get(
        get_node_id.options(
            scheduling_strategy=NodeAffinitySchedulingStrategy(
                worker_node_id, soft=False
            )
        ).remote()
    )
    assert head_node_id == ray.get(
        get_node_id.options(
            scheduling_strategy=NodeAffinitySchedulingStrategy(head_node_id, soft=False)
        ).remote()
    )

    # Doesn't fail when the node doesn't exist since soft is true.
    ray.get(
        get_node_id.options(
            scheduling_strategy=NodeAffinitySchedulingStrategy(
                ray.NodeID.from_random().hex(), soft=True
            )
        ).remote()
    )

    # Doesn't fail when the node is infeasible since soft is true.
    assert worker_node_id == ray.get(
        get_node_id.options(
            scheduling_strategy=NodeAffinitySchedulingStrategy(head_node_id, soft=True),
            resources={"worker": 1},
        ).remote()
    )

    # Fail when the node doesn't exist.
    with pytest.raises(ray.exceptions.TaskUnschedulableError):
        ray.get(
            get_node_id.options(
                scheduling_strategy=NodeAffinitySchedulingStrategy(
                    ray.NodeID.from_random().hex(), soft=False
                )
            ).remote()
        )

    # Fail when the node is infeasible.
    with pytest.raises(ray.exceptions.TaskUnschedulableError):
        ray.get(
            get_node_id.options(
                scheduling_strategy=NodeAffinitySchedulingStrategy(
                    head_node_id, soft=False
                ),
                resources={"not_exist": 1},
            ).remote()
        )

    crashed_worker_node = cluster.add_node(num_cpus=8, resources={"crashed_worker": 1})
    cluster.wait_for_nodes()
    crashed_worker_node_id = ray.get(
        get_node_id.options(num_cpus=0, resources={"crashed_worker": 1}).remote()
    )

    @ray.remote(
        max_retries=-1,
        scheduling_strategy=NodeAffinitySchedulingStrategy(
            crashed_worker_node_id, soft=True
        ),
    )
    def crashed_get_node_id():
        if ray.get_runtime_context().get_node_id() == crashed_worker_node_id:
            internal_kv._internal_kv_put(
                "crashed_get_node_id", "crashed_worker_node_id"
            )
            while True:
                time.sleep(1)
        else:
            return ray.get_runtime_context().get_node_id()

    r = crashed_get_node_id.remote()
    while not internal_kv._internal_kv_exists("crashed_get_node_id"):
        time.sleep(0.1)
    cluster.remove_node(crashed_worker_node, allow_graceful=False)
    assert ray.get(r) in {head_node_id, worker_node_id}

    @ray.remote(num_cpus=1)
    class Actor:
        def get_node_id(self):
            return ray.get_runtime_context().get_node_id()

    actor = Actor.options(
        scheduling_strategy=NodeAffinitySchedulingStrategy(worker_node_id, soft=False)
    ).remote()
    assert worker_node_id == ray.get(actor.get_node_id.remote())

    actor = Actor.options(
        scheduling_strategy=NodeAffinitySchedulingStrategy(head_node_id, soft=False)
    ).remote()
    assert head_node_id == ray.get(actor.get_node_id.remote())

    actor = Actor.options(
        scheduling_strategy=NodeAffinitySchedulingStrategy(worker_node_id, soft=False),
        num_cpus=0,
    ).remote()
    assert worker_node_id == ray.get(actor.get_node_id.remote())

    actor = Actor.options(
        scheduling_strategy=NodeAffinitySchedulingStrategy(head_node_id, soft=False),
        num_cpus=0,
    ).remote()
    assert head_node_id == ray.get(actor.get_node_id.remote())

    # Wait until the target node becomes available.
    worker_actor = Actor.options(resources={"worker": 1}).remote()
    assert worker_node_id == ray.get(worker_actor.get_node_id.remote())
    actor = Actor.options(
        scheduling_strategy=NodeAffinitySchedulingStrategy(worker_node_id, soft=True),
        resources={"worker": 1},
    ).remote()
    del worker_actor
    assert worker_node_id == ray.get(actor.get_node_id.remote())

    # Doesn't fail when the node doesn't exist since soft is true.
    actor = Actor.options(
        scheduling_strategy=NodeAffinitySchedulingStrategy(
            ray.NodeID.from_random().hex(), soft=True
        )
    ).remote()
    assert ray.get(actor.get_node_id.remote())

    # Doesn't fail when the node is infeasible since soft is true.
    actor = Actor.options(
        scheduling_strategy=NodeAffinitySchedulingStrategy(head_node_id, soft=True),
        resources={"worker": 1},
    ).remote()
    assert worker_node_id == ray.get(actor.get_node_id.remote())

    # Fail when the node doesn't exist.
    with pytest.raises(ray.exceptions.ActorUnschedulableError):
        actor = Actor.options(
            scheduling_strategy=NodeAffinitySchedulingStrategy(
                ray.NodeID.from_random().hex(), soft=False
            )
        ).remote()
        ray.get(actor.get_node_id.remote())

    # Fail when the node is infeasible.
    with pytest.raises(ray.exceptions.ActorUnschedulableError):
        actor = Actor.options(
            scheduling_strategy=NodeAffinitySchedulingStrategy(
                worker_node_id, soft=False
            ),
            resources={"not_exist": 1},
        ).remote()
        ray.get(actor.get_node_id.remote())


def test_node_affinity_scheduling_strategy_soft_spill_on_unavailable(ray_start_cluster):
    cluster = ray_start_cluster
    head_node = cluster.add_node(num_cpus=1, resources={"custom": 1})
    worker_node = cluster.add_node(num_cpus=1, resources={"custom": 1})
    cluster.wait_for_nodes()

    ray.init(address=cluster.address)

    signal = SignalActor.remote()

    # NOTE: need to include custom resource because CPUs are released during `ray.get`.
    @ray.remote(
        num_cpus=1,
        resources={"custom": 1},
    )
    def get_node_id() -> str:
        ray.get(signal.wait.remote())
        return ray.get_runtime_context().get_node_id()

    # Submit a first task that has affinity to the worker node.
    # It should be placed on the worker node and occupy the resources.
    worker_node_ref = get_node_id.options(
        scheduling_strategy=NodeAffinitySchedulingStrategy(
            worker_node.node_id,
            soft=False,
        ),
    ).remote()

    wait_for_condition(lambda: ray.get(signal.cur_num_waiters.remote()) == 1)

    # Submit a second task that has soft affinity to the worker node.
    # It should be spilled to the head node.
    head_node_ref = get_node_id.options(
        scheduling_strategy=NodeAffinitySchedulingStrategy(
            worker_node.node_id,
            soft=True,
            _spill_on_unavailable=True,
        ),
    ).remote()
    ray.get(signal.send.remote())

    assert ray.get(head_node_ref, timeout=10) == head_node.node_id
    assert ray.get(worker_node_ref, timeout=10) == worker_node.node_id


def test_node_affinity_scheduling_strategy_fail_on_unavailable(ray_start_cluster):
    cluster = ray_start_cluster
    cluster.add_node(num_cpus=1)
    ray.init(address=cluster.address)

    @ray.remote(num_cpus=1)
    class Actor:
        def get_node_id(self):
            return ray.get_runtime_context().get_node_id()

    a1 = Actor.remote()
    target_node_id = ray.get(a1.get_node_id.remote())

    a2 = Actor.options(
        scheduling_strategy=NodeAffinitySchedulingStrategy(
            target_node_id, soft=False, _fail_on_unavailable=True
        )
    ).remote()

    with pytest.raises(ray.exceptions.ActorUnschedulableError):
        ray.get(a2.get_node_id.remote())


def test_spread_scheduling_strategy(ray_start_cluster):
    cluster = ray_start_cluster
    # Create a head node
    cluster.add_node(
        num_cpus=0,
        _system_config={
            "scheduler_spread_threshold": 1,
        },
    )
    ray.init(address=cluster.address)
    for i in range(2):
        cluster.add_node(num_cpus=8, resources={f"foo:{i}": 1})
    cluster.wait_for_nodes()

    @ray.remote
    def get_node_id():
        return ray.get_runtime_context().get_node_id()

    worker_node_ids = {
        ray.get(get_node_id.options(resources={f"foo:{i}": 1}).remote())
        for i in range(2)
    }
    # Wait for updating driver raylet's resource view.
    time.sleep(5)

    @ray.remote(scheduling_strategy="SPREAD")
    def task1():
        internal_kv._internal_kv_put("test_task1", "task1")
        while internal_kv._internal_kv_exists("test_task1"):
            time.sleep(0.1)
        return ray.get_runtime_context().get_node_id()

    @ray.remote
    def task2():
        internal_kv._internal_kv_put("test_task2", "task2")
        return ray.get_runtime_context().get_node_id()

    locations = []
    locations.append(task1.remote())
    while not internal_kv._internal_kv_exists("test_task1"):
        time.sleep(0.1)
    # Wait for updating driver raylet's resource view.
    time.sleep(5)
    locations.append(task2.options(scheduling_strategy="SPREAD").remote())
    while not internal_kv._internal_kv_exists("test_task2"):
        time.sleep(0.1)
    internal_kv._internal_kv_del("test_task1")
    internal_kv._internal_kv_del("test_task2")
    assert set(ray.get(locations)) == worker_node_ids

    # Wait for updating driver raylet's resource view.
    time.sleep(5)

    # Make sure actors can be spreaded as well.
    @ray.remote(num_cpus=1)
    class Actor:
        def ping(self):
            return ray.get_runtime_context().get_node_id()

    actors = []
    locations = []
    for i in range(8):
        actors.append(Actor.options(scheduling_strategy="SPREAD").remote())
        locations.append(ray.get(actors[-1].ping.remote()))
    locations.sort()
    expected_locations = list(worker_node_ids) * 4
    expected_locations.sort()
    assert locations == expected_locations


@pytest.mark.skipif(
    platform.system() == "Windows", reason="FakeAutoscaler doesn't work on Windows"
)
@pytest.mark.parametrize("autoscaler_v2", [False, True], ids=["v1", "v2"])
def test_demand_report_for_node_affinity_scheduling_strategy(
    autoscaler_v2, monkeypatch, shutdown_only
):
    from ray.cluster_utils import AutoscalingCluster

    cluster = AutoscalingCluster(
        head_resources={"CPU": 0},
        worker_node_types={
            "cpu_node": {
                "resources": {
                    "CPU": 1,
                    "object_store_memory": 1024 * 1024 * 1024,
                },
                "node_config": {},
                "min_workers": 1,
                "max_workers": 1,
            },
        },
        autoscaler_v2=autoscaler_v2,
    )

    cluster.start()
    info = ray.init(address="auto")

    @ray.remote(num_cpus=1)
    def f(sleep_s):
        time.sleep(sleep_s)
        return ray.get_runtime_context().get_node_id()

    worker_node_id = ray.get(f.remote(0))

    tasks = []
    tasks.append(f.remote(10000))
    # This is not reported since there is feasible node.
    tasks.append(
        f.options(
            scheduling_strategy=NodeAffinitySchedulingStrategy(
                worker_node_id, soft=False
            )
        ).remote(0)
    )
    # This is reported since there is no feasible node and soft is True.
    tasks.append(
        f.options(
            num_gpus=1,
            scheduling_strategy=NodeAffinitySchedulingStrategy(
                ray.NodeID.from_random().hex(), soft=True
            ),
        ).remote(0)
    )

    global_state_accessor = make_global_state_accessor(info)

    def check_resource_demand():
        message = global_state_accessor.get_all_resource_usage()
        if message is None:
            return False

        resource_usage = gcs_utils.ResourceUsageBatchData.FromString(message)
        aggregate_resource_load = resource_usage.resource_load_by_shape.resource_demands

        if len(aggregate_resource_load) != 1:
            return False

        if aggregate_resource_load[0].num_infeasible_requests_queued != 1:
            return False

        if aggregate_resource_load[0].shape != {"CPU": 1.0, "GPU": 1.0}:
            return False

        return True

    wait_for_condition(check_resource_demand, 20)
    cluster.shutdown()


@pytest.mark.skipif(
    platform.system() == "Windows", reason="FakeAutoscaler doesn't work on Windows"
)
@pytest.mark.skipif(os.environ.get("ASAN_OPTIONS") is not None, reason="ASAN is slow")
@pytest.mark.parametrize("autoscaler_v2", [True, False], ids=["v2", "v1"])
def test_demand_report_when_scale_up(autoscaler_v2, shutdown_only):
    # https://github.com/ray-project/ray/issues/22122
    from ray.cluster_utils import AutoscalingCluster

    cluster = AutoscalingCluster(
        head_resources={"CPU": 0},
        worker_node_types={
            "cpu_node": {
                "resources": {
                    "CPU": 1,
                    "object_store_memory": 1024 * 1024 * 1024,
                },
                "node_config": {},
                "min_workers": 2,
                "max_workers": 2,
            },
        },
        autoscaler_v2=autoscaler_v2,
        max_workers=4,  # default 8
        upscaling_speed=5,  # greater upscaling speed
    )

    cluster.start()

    info = ray.init("auto")

    @ray.remote
    def f():
        time.sleep(10000)

    @ray.remote
    def g():
        ray.get(h.remote())

    @ray.remote
    def h():
        time.sleep(10000)

    tasks = [f.remote() for _ in range(500)] + [
        g.remote() for _ in range(500)
    ]  # noqa: F841

    global_state_accessor = make_global_state_accessor(info)

    def check_backlog_info():
        message = global_state_accessor.get_all_resource_usage()
        if message is None:
            return 0

        resource_usage = gcs_utils.ResourceUsageBatchData.FromString(message)
        aggregate_resource_load = resource_usage.resource_load_by_shape.resource_demands

        if len(aggregate_resource_load) != 1:
            return False

        (backlog_size, num_ready_requests_queued, shape) = (
            aggregate_resource_load[0].backlog_size,
            aggregate_resource_load[0].num_ready_requests_queued,
            aggregate_resource_load[0].shape,
        )
        # The expected backlog sum is 998, which is derived from the total number of tasks
        # (1000) minus the number of active workers (2). This ensures the test validates
        # the correct backlog size and queued requests.
        if backlog_size + num_ready_requests_queued != 998:
            return False

        if shape != {"CPU": 1.0}:
            return False
        return True

    # In ASAN test it's slow.
    # Wait for 20s for the cluster to be up
    try:
        wait_for_condition(check_backlog_info, 20)
    except RuntimeError:
        tasks = list_tasks(limit=10000)
        print(f"Total tasks: {len(tasks)}")
        for task in tasks:
            print(task)
        raise
    cluster.shutdown()
    ray.shutdown()


@pytest.mark.skipif(
    ray._private.client_mode_hook.is_client_mode_enabled, reason="Fails w/ Ray Client."
)
def test_data_locality_spilled_objects(
    ray_start_cluster_enabled, fs_only_object_spilling_config
):
    cluster = ray_start_cluster_enabled
    object_spilling_config, _ = fs_only_object_spilling_config
    cluster.add_node(
        num_cpus=1,
        object_store_memory=100 * 1024 * 1024,
        _system_config={
            "min_spilling_size": 1,
            "object_spilling_config": object_spilling_config,
        },
    )
    ray.init(cluster.address)
    cluster.add_node(
        num_cpus=1, object_store_memory=100 * 1024 * 1024, resources={"remote": 1}
    )

    @ray.remote(resources={"remote": 1})
    def f():
        return (
            np.zeros(50 * 1024 * 1024, dtype=np.uint8),
            ray.runtime_context.get_runtime_context().get_node_id(),
        )

    @ray.remote
    def check_locality(x):
        _, node_id = x
        assert node_id == ray.runtime_context.get_runtime_context().get_node_id()

    # Check locality works when dependent task is already submitted by the time
    # the upstream task finishes.
    for _ in range(5):
        ray.get(check_locality.remote(f.remote()))

    # Check locality works when some objects were spilled.
    xs = [f.remote() for _ in range(5)]
    ray.wait(xs, num_returns=len(xs), fetch_local=False)
    for i, x in enumerate(xs):
        task = check_locality.remote(x)
        print(i, x, task)
        ray.get(task)


@pytest.mark.skipif(platform.system() == "Windows", reason="Metrics flake on Windows.")
def test_workload_placement_metrics(ray_start_regular):
    @ray.remote(num_cpus=1)
    def task():
        pass

    @ray.remote(num_cpus=1)
    class Actor:
        def ready(self):
            return True

    t = task.remote()
    ray.get(t)
    a = Actor.remote()
    ray.get(a.ready.remote())
    del a
    pg = placement_group(bundles=[{"CPU": 1}], strategy="SPREAD")
    ray.get(pg.ready())

    placement_metric_condition = get_metric_check_condition(
        [
            MetricSamplePattern(
                name="ray_scheduler_placement_time_s_bucket",
                value=1.0,
                partial_label_match={"WorkloadType": "Actor"},
            ),
            MetricSamplePattern(
                name="ray_scheduler_placement_time_s_bucket",
                value=1.0,
                partial_label_match={"WorkloadType": "Task"},
            ),
            MetricSamplePattern(
                name="ray_scheduler_placement_time_s_bucket",
                value=1.0,
                partial_label_match={"WorkloadType": "PlacementGroup"},
            ),
        ],
    )
    wait_for_condition(placement_metric_condition, timeout=60)


def test_negative_resource_availability(shutdown_only):
    """Test pg scheduling when resource availability is negative."""
    ray.init(num_cpus=1)

    signal1 = SignalActor.remote()
    signal2 = SignalActor.remote()

    @ray.remote(num_cpus=0)
    def child(signal1):
        ray.get(signal1.wait.remote())

    @ray.remote(num_cpus=1)
    def parent(signal1, signal2):
        # Release the CPU resource,
        # the resource will be acquired by Actor.
        ray.get(child.remote(signal1))
        # Re-acquire the CPU resource
        # the availability should be -1 afterwards.
        signal2.send.remote()
        while True:
            time.sleep(1)

    @ray.remote(num_cpus=1)
    class Actor:
        def ping(self):
            return "hello"

    parent.remote(signal1, signal2)
    actor = Actor.remote()
    ray.get(actor.ping.remote())
    signal1.send.remote()
    ray.get(signal2.wait.remote())
    # CPU resource availability should be negative now
    # and the pg should be pending.
    pg = placement_group([{"CPU": 1}])
    with pytest.raises(ray.exceptions.GetTimeoutError):
        ray.get(pg.ready(), timeout=2)


if __name__ == "__main__":
    sys.exit(pytest.main(["-sv", __file__]))
