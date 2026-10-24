import json
import unittest

from scheduler import disable_job


class SchedulerTests(unittest.TestCase):
    def test_disables_only_the_configured_job(self):
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                return b"{}"

        def open_request(request, timeout):
            requests.append(request)
            self.assertEqual(timeout, 20)
            return Response()

        disable_job("test-api-key", "12345", opener=open_request)
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.full_url, "https://api.cron-job.org/jobs/12345")
        self.assertEqual(request.method, "PATCH")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-api-key")
        self.assertEqual(json.loads(request.data), {"job": {"enabled": False}})

    def test_rejects_invalid_job_identifiers_before_sending_requests(self):
        for job_id in ["", "0", "-1", "../jobs", "1/other"]:
            with self.subTest(job_id=job_id):
                with self.assertRaises(ValueError):
                    disable_job("test-api-key", job_id, opener=lambda *_: self.fail("Unexpected request"))

    def test_rejects_an_empty_api_key(self):
        with self.assertRaises(ValueError):
            disable_job("", "12345", opener=lambda *_: self.fail("Unexpected request"))


if __name__ == "__main__":
    unittest.main()
