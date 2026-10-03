from humble_bundle_keys.operation_report import OperationReport


def test_counts_deduplicate_fallback_and_keep_saved_success(tmp_path):
    report = OperationReport("reveal")
    for identity in ("a", "b", "c", "d"):
        report.add(identity, identity)
    report.result("a", "failed")
    report.add("a", "a")
    report.result("a", "success")
    report.result("a", "failed", "refresh failed")
    report.result("b", "skipped", "expired")
    report.result("c", "failed", "no key returned")
    report.finish()
    path = tmp_path / "report.json"
    report.save(path)
    view = OperationReport.load(path).view()
    assert (view["total"], view["processed"], view["succeeded"], view["skipped"],
            view["failed"], view["remaining"]) == (4, 3, 1, 1, 1, 1)
    assert view["status"] == "partial"


def test_stop_and_restart_keep_unfinished_items(tmp_path):
    report = OperationReport("claim-months")
    report.add("a", "A")
    report.add("b", "B")
    report.result("a", "success")
    path = tmp_path / "report.json"
    report.save(path)
    recovered = OperationReport.load(path)
    assert recovered.view()["status"] == "interrupted"
    assert recovered.view()["succeeded"] == recovered.view()["remaining"] == 1
    report.finish(stopped=True)
    assert report.view()["status"] == "stopped"
    assert report.view()["processed"] == 1
