import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

from humble_bundle_keys.month_claim import claim_months, save_modal_key
from humble_bundle_keys.web import Inventory


def target(index, policy="all_games"):
    return {"url": f"https://www.humblebundle.com/membership/january-{2022 + index}",
            "bundle_name": f"Synthetic month {index}", "titles": [f"Game {index}"],
            "choice_policy": policy, "choices_remaining": 1}


def test_two_month_workers_and_limited_month_waits(tmp_path, monkeypatch):
    inv = Inventory(tmp_path)
    lock, barrier = threading.Lock(), threading.Barrier(2)
    active = peak = completed = 0

    def worker(state, inventory, month):
        nonlocal active, peak, completed
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=3)
        with lock:
            active -= 1
            completed += 1
        return True

    def serial(context, inventory, months):
        assert completed == 2 and active == 0
        assert months[0]["choice_policy"] == "limited"
        return True

    monkeypatch.setattr("humble_bundle_keys.month_claim._claim_worker", worker)
    monkeypatch.setattr("humble_bundle_keys.month_claim._claim_serial", serial)
    claim_months(MagicMock(), inv, [target(0), target(1), target(2, "limited")])
    assert peak == 2


def test_uncertain_result_does_not_launch_next_month(tmp_path, monkeypatch):
    inv = Inventory(tmp_path)
    started = []
    barrier = threading.Barrier(2)

    def worker(state, inventory, month):
        started.append(month["url"])
        barrier.wait(timeout=3)
        if month["url"] == target(0)["url"]:
            return False
        assert inventory.stop_event.wait(3)
        return None

    monkeypatch.setattr("humble_bundle_keys.month_claim._claim_worker", worker)
    claim_months(MagicMock(), inv, [target(i) for i in range(4)])
    assert len(started) == 2 and inv.stop_event.is_set()


def test_parallel_saved_keys_do_not_overwrite_each_other(tmp_path):
    inv = Inventory(tmp_path)
    def save(index):
        order = {"gamekey": f"synthetic-{index}",
                 "product": {"human_name": f"Month {index}"}, "tpkd_dict": {"all_tpks": []}}
        save_modal_key(inv, order, f"Game {index}", f"SYNTHETIC-KEY-{index}")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(save, range(10)))
    assert len(Inventory(tmp_path).snapshot["rows"]) == 10
