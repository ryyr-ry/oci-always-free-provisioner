import unittest

from provision import Config, FatalError, Result, RetryableError, provision_once, run_retry_loop


def config(**overrides):
    values = dict(
        tenancy_id="tenancy", compartment_id="compartment", subnet_id="subnet",
        availability_domain="home-ad", image_id="ubuntu-arm64", ssh_public_key="ssh-ed25519 public",
        region="ap-tokyo-1", home_region="ap-tokyo-1",
    )
    values.update(overrides)
    return Config(**values)


class FakeBackend:
    def __init__(self):
        self.instance = None
        self.volume = None
        self.attachment = None
        self.public_ip = None
        self.storage_available = 200
        self.a1_ocpus = 0
        self.a1_memory = 0
        self.boot_size = 50
        self.launch_error = None
        self.created = []

    def find_instance(self, cfg):
        return self.instance

    def free_storage_gb(self, cfg):
        return self.storage_available

    def a1_usage(self, cfg):
        return self.a1_ocpus, self.a1_memory

    def launch_instance(self, cfg):
        self.created.append(("instance", cfg.token("instance")))
        if self.launch_error:
            raise self.launch_error
        self.instance = {"id": "instance", "state": "RUNNING", "ocpus": 2, "memory_gb": 12, "shape": "VM.Standard.A1.Flex"}
        self.storage_available -= 50
        return self.instance

    def boot_volume_gb(self, cfg, instance):
        return self.boot_size

    def find_volume(self, cfg):
        return self.volume

    def create_volume(self, cfg):
        self.created.append(("volume", cfg.token("volume")))
        self.volume = {"id": "volume", "state": "AVAILABLE", "size_gb": 100}
        self.storage_available -= 100
        return self.volume

    def find_attachment(self, cfg, instance, volume):
        return self.attachment

    def attach_volume(self, cfg, instance, volume):
        self.created.append(("attachment", None))
        self.attachment = {"id": "attachment", "state": "ATTACHED"}
        return self.attachment

    def primary_private_ip(self, cfg, instance):
        return "private-ip"

    def find_public_ip(self, cfg):
        return self.public_ip

    def create_public_ip(self, cfg, private_ip):
        self.created.append(("public-ip", None))
        self.public_ip = {"id": "public-ip", "state": "ASSIGNED", "private_ip_id": private_ip, "address": "192.0.2.10"}
        return self.public_ip

    def assign_public_ip(self, public_ip, private_ip):
        self.created.append(("assign-public-ip", None))
        self.public_ip.update(state="ASSIGNED", private_ip_id=private_ip)
        return self.public_ip


class ProvisionTests(unittest.TestCase):
    def test_provisions_requested_resources_and_stops_creating_on_rerun(self):
        backend = FakeBackend()
        first = provision_once(backend, config())
        self.assertTrue(first.ready)
        self.assertEqual(first.public_ip, "192.0.2.10")
        self.assertEqual([kind for kind, _ in backend.created], ["instance", "volume", "attachment", "public-ip"])
        second = provision_once(backend, config())
        self.assertTrue(second.ready)
        self.assertEqual(len(backend.created), 4)

    def test_capacity_error_does_not_allocate_storage_or_public_ip(self):
        backend = FakeBackend()
        backend.launch_error = RetryableError("Out of host capacity")
        with self.assertRaises(RetryableError):
            provision_once(backend, config())
        self.assertEqual([kind for kind, _ in backend.created], ["instance"])

    def test_insufficient_free_storage_prevents_launch(self):
        backend = FakeBackend()
        backend.storage_available = 149
        with self.assertRaises(FatalError):
            provision_once(backend, config())
        self.assertEqual(backend.created, [])

    def test_other_a1_allocations_prevent_exceeding_the_free_compute_budget(self):
        for ocpus, memory in [(1, 0), (0, 1)]:
            backend = FakeBackend()
            backend.a1_ocpus, backend.a1_memory = ocpus, memory
            with self.subTest(ocpus=ocpus, memory=memory):
                with self.assertRaises(FatalError):
                    provision_once(backend, config())
                self.assertEqual(backend.created, [])

    def test_provisioning_instance_is_waited_for_without_a_second_launch(self):
        backend = FakeBackend()
        provision_once(backend, config())
        backend.instance["state"] = "PROVISIONING"
        backend.created.clear()
        self.assertFalse(provision_once(backend, config()).ready)
        self.assertEqual(backend.created, [])

    def test_terminated_instance_is_not_recreated(self):
        backend = FakeBackend()
        provision_once(backend, config())
        backend.instance["state"] = "TERMINATED"
        backend.created.clear()
        with self.assertRaises(FatalError):
            provision_once(backend, config())
        self.assertEqual(backend.created, [])

    def test_mismatching_existing_instance_is_not_modified(self):
        backend = FakeBackend()
        provision_once(backend, config())
        backend.instance["memory_gb"] = 24
        backend.created.clear()
        with self.assertRaises(FatalError):
            provision_once(backend, config())
        self.assertEqual(backend.created, [])

    def test_existing_volume_must_match_the_requested_size(self):
        backend = FakeBackend()
        provision_once(backend, config())
        backend.volume["size_gb"] = 200
        with self.assertRaises(FatalError):
            provision_once(backend, config())

    def test_pending_volume_does_not_create_duplicate_storage(self):
        backend = FakeBackend()
        provision_once(backend, config())
        backend.volume["state"] = "PROVISIONING"
        backend.created.clear()
        self.assertFalse(provision_once(backend, config()).ready)
        self.assertEqual(backend.created, [])

    def test_pending_attachment_is_not_attached_twice(self):
        backend = FakeBackend()
        provision_once(backend, config())
        backend.attachment["state"] = "ATTACHING"
        backend.created.clear()
        self.assertFalse(provision_once(backend, config()).ready)
        self.assertEqual(backend.created, [])

    def test_a_reserved_address_assigned_elsewhere_is_not_moved(self):
        backend = FakeBackend()
        provision_once(backend, config())
        backend.public_ip["private_ip_id"] = "someone-else"
        with self.assertRaises(FatalError):
            provision_once(backend, config())

    def test_a_detached_reserved_address_is_reused(self):
        backend = FakeBackend()
        provision_once(backend, config())
        backend.public_ip.update(private_ip_id=None, state="AVAILABLE")
        backend.created.clear()
        self.assertTrue(provision_once(backend, config()).ready)
        self.assertEqual(backend.created, [("assign-public-ip", None)])

    def test_unexpected_boot_size_stops_further_provisioning(self):
        backend = FakeBackend()
        backend.boot_size = 100
        with self.assertRaises(FatalError):
            provision_once(backend, config())
        self.assertNotIn("volume", [kind for kind, _ in backend.created])


class RetryTests(unittest.TestCase):
    def run_loop(self, attempts, duration=180, operation_seconds=0):
        now = [0.0]
        starts = []

        def attempt():
            starts.append(now[0])
            now[0] += operation_seconds
            value = attempts.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        def sleep(seconds):
            now[0] += seconds

        result = run_retry_loop(attempt, window_seconds=duration, clock=lambda: now[0], sleep=sleep, log=lambda _: None)
        return result, starts

    def test_retries_every_minute_and_stops_immediately_on_success(self):
        result, starts = self.run_loop([RetryableError("capacity"), Result(False, "pending"), Result(True, "ready")])
        self.assertTrue(result.ready)
        self.assertEqual(starts, [0.0, 60.0, 120.0])

    def test_request_time_is_included_in_the_retry_interval(self):
        result, starts = self.run_loop([RetryableError("capacity"), Result(True, "ready")], operation_seconds=7)
        self.assertTrue(result.ready)
        self.assertEqual(starts, [0.0, 60.0])

    def test_retry_window_is_bounded(self):
        result, starts = self.run_loop([RetryableError("capacity")] * 3)
        self.assertFalse(result.ready)
        self.assertEqual(starts, [0.0, 60.0, 120.0])

    def test_authentication_or_configuration_errors_are_not_retried(self):
        with self.assertRaises(FatalError):
            self.run_loop([FatalError("authentication")])


class ConfigurationTests(unittest.TestCase):
    def test_idempotency_tokens_survive_process_restarts_and_differ_by_resource(self):
        self.assertEqual(config().token("instance"), config().token("instance"))
        self.assertNotEqual(config().token("instance"), config().token("volume"))

    def test_non_home_regions_and_larger_paid_configurations_are_rejected(self):
        for values in [{"region": "other"}, {"ocpus": 4}, {"memory_gb": 24}, {"boot_gb": 100}, {"data_gb": 150}]:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    config(**values)


if __name__ == "__main__":
    unittest.main()
