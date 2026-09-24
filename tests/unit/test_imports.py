def test_phase_one_runtime_dependencies_import() -> None:
    import agent_framework  # noqa: F401
    import playwright.async_api  # noqa: F401

