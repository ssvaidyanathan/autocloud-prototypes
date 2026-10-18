from agent.app.tools.runtime_tools import (
    build_cloud_logging_filter,
    query_cloud_logging,
    reset_runtime_state,
)


class TestCloudLoggingFilter:
    def setup_method(self) -> None:
        reset_runtime_state()

    def test_structured_filter_construction_exact_string(self) -> None:
        """Verify exact filter string produced for revision-scoped queries uses canonical syntax."""
        filter_str = build_cloud_logging_filter(
            service_name="sample-pipeline-app",
            revision_name="sample-pipeline-app-00007-748",
            start_time="2026-10-17T11:22:04Z",
            end_time="2026-10-17T11:52:04Z",
            severity="DEFAULT",
        )
        assert 'resource.type="cloud_run_revision"' in filter_str
        assert 'resource.labels.service_name="sample-pipeline-app"' in filter_str
        assert 'resource.labels.revision_name="sample-pipeline-app-00007-748"' in filter_str
        assert 'timestamp>="2026-10-17T11:22:04Z"' in filter_str
        assert 'timestamp<="2026-10-17T11:52:04Z"' in filter_str
        assert 'severity>="DEFAULT"' in filter_str
        # MUST NOT contain bare revision_name or service_name without resource.labels.
        assert " resource.labels.revision_name=" in filter_str or filter_str.startswith(
            "resource.labels.revision_name="
        )
        assert " resource.labels.service_name=" in filter_str or filter_str.startswith(
            "resource.labels.service_name="
        )
        # Ensure no un-prefixed revision_name= exists
        tokens = filter_str.split()
        for token in tokens:
            if "revision_name=" in token:
                assert token.startswith('resource.labels.revision_name="')
            if "service_name=" in token:
                assert token.startswith('resource.labels.service_name="')

    def test_raw_filter_repair_converts_bare_revision_name(self) -> None:
        """Verify raw LLM filter string with bare revision_name and service_name is repaired."""
        raw_llm_filter = (
            'resource.type="cloud_run_revision" revision_name="sample-pipeline-app-00007-748" '
            'service_name="sample-pipeline-app" timestamp>="2026-10-17T11:22:04Z" '
            'timestamp<="2026-10-17T11:52:04Z"'
        )
        repaired = build_cloud_logging_filter(filter_query=raw_llm_filter)
        assert 'resource.labels.revision_name="sample-pipeline-app-00007-748"' in repaired
        assert 'resource.labels.service_name="sample-pipeline-app"' in repaired
        assert "revision_name=" not in repaired.replace("resource.labels.revision_name=", "")
        assert "service_name=" not in repaired.replace("resource.labels.service_name=", "")

    def test_raw_filter_already_valid_preserved(self) -> None:
        """Verify already valid GCP filter syntax is preserved without double-prefixing."""
        valid_filter = (
            'resource.type="cloud_run_revision" '
            'resource.labels.service_name="sample-pipeline-app" '
            'resource.labels.revision_name="sample-pipeline-app-00007-748"'
        )
        repaired = build_cloud_logging_filter(filter_query=valid_filter)
        assert repaired == valid_filter
        assert "resource.labels.resource.labels." not in repaired

    def test_query_cloud_logging_with_structured_parameters_and_client(self) -> None:
        """Verify query_cloud_logging executes with repaired filter using supplied client."""
        recorded_filter = []

        class FakeLoggingClient:
            def list_entries(self, filter_: str, page_size: int = 50):
                recorded_filter.append(filter_)
                return [
                    {
                        "timestamp": "2026-10-17T11:36:59Z",
                        "severity": "ERROR",
                        "text_payload": "Memory limit of 512 MiB exceeded with 1160 MiB used.",
                    }
                ]

        client = FakeLoggingClient()
        entries = query_cloud_logging(
            service_name="sample-pipeline-app",
            revision_name="sample-pipeline-app-00007-748",
            client=client,
        )
        assert len(entries) == 1
        assert "Memory limit of 512 MiB exceeded" in entries[0]["text_payload"]
        assert len(recorded_filter) == 1
        assert 'resource.labels.revision_name="sample-pipeline-app-00007-748"' in recorded_filter[0]
