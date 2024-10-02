from __future__ import annotations

from typing import TypeVar

import pytest

import trio
from trio.lowlevel import (
    add_parking_lot_breaker,
    current_task,
    remove_parking_lot_breaker,
)
from trio.testing import Matcher, RaisesGroup

from ... import _core
from ...testing import wait_all_tasks_blocked
from .._parking_lot import ParkingLot
from .tutil import check_sequence_matches

T = TypeVar("T")


async def test_parking_lot_basic() -> None:
    record = []

    async def waiter(i: int, lot: ParkingLot) -> None:
        record.append(f"sleep {i}")
        await lot.park()
        record.append(f"wake {i}")

    async with _core.open_nursery() as nursery:
        lot = ParkingLot()
        assert not lot
        assert len(lot) == 0
        assert lot.statistics().tasks_waiting == 0
        for i in range(3):
            nursery.start_soon(waiter, i, lot)
        await wait_all_tasks_blocked()
        assert len(record) == 3
        assert bool(lot)
        assert len(lot) == 3
        assert lot.statistics().tasks_waiting == 3
        lot.unpark_all()
        assert lot.statistics().tasks_waiting == 0
        await wait_all_tasks_blocked()
        assert len(record) == 6

    check_sequence_matches(
        record,
        [{"sleep 0", "sleep 1", "sleep 2"}, {"wake 0", "wake 1", "wake 2"}],
    )

    async with _core.open_nursery() as nursery:
        record = []
        for i in range(3):
            nursery.start_soon(waiter, i, lot)
            await wait_all_tasks_blocked()
        assert len(record) == 3
        for _ in range(3):
            lot.unpark()
            await wait_all_tasks_blocked()
        # 1-by-1 wakeups are strict FIFO
        assert record == [
            "sleep 0",
            "sleep 1",
            "sleep 2",
            "wake 0",
            "wake 1",
            "wake 2",
        ]

    # It's legal (but a no-op) to try and unpark while there's nothing parked
    lot.unpark()
    lot.unpark(count=1)
    lot.unpark(count=100)

    # Check unpark with count
    async with _core.open_nursery() as nursery:
        record = []
        for i in range(3):
            nursery.start_soon(waiter, i, lot)
            await wait_all_tasks_blocked()
        lot.unpark(count=2)
        await wait_all_tasks_blocked()
        check_sequence_matches(
            record,
            ["sleep 0", "sleep 1", "sleep 2", {"wake 0", "wake 1"}],
        )
        lot.unpark_all()

    with pytest.raises(
        ValueError,
        match=r"^Cannot pop a non-integer number of tasks\.$",
    ):
        lot.unpark(count=1.5)


async def cancellable_waiter(
    name: T,
    lot: ParkingLot,
    scopes: dict[T, _core.CancelScope],
    record: list[str],
) -> None:
    with _core.CancelScope() as scope:
        scopes[name] = scope
        record.append(f"sleep {name}")
        try:
            await lot.park()
        except _core.Cancelled:
            record.append(f"cancelled {name}")
        else:
            record.append(f"wake {name}")


async def test_parking_lot_cancel() -> None:
    record: list[str] = []
    scopes: dict[int, _core.CancelScope] = {}

    async with _core.open_nursery() as nursery:
        lot = ParkingLot()
        nursery.start_soon(cancellable_waiter, 1, lot, scopes, record)
        await wait_all_tasks_blocked()
        nursery.start_soon(cancellable_waiter, 2, lot, scopes, record)
        await wait_all_tasks_blocked()
        nursery.start_soon(cancellable_waiter, 3, lot, scopes, record)
        await wait_all_tasks_blocked()
        assert len(record) == 3

        scopes[2].cancel()
        await wait_all_tasks_blocked()
        assert len(record) == 4
        lot.unpark_all()
        await wait_all_tasks_blocked()
        assert len(record) == 6

    check_sequence_matches(
        record,
        ["sleep 1", "sleep 2", "sleep 3", "cancelled 2", {"wake 1", "wake 3"}],
    )


async def test_parking_lot_repark() -> None:
    record: list[str] = []
    scopes: dict[int, _core.CancelScope] = {}
    lot1 = ParkingLot()
    lot2 = ParkingLot()

    with pytest.raises(TypeError):
        lot1.repark([])  # type: ignore[arg-type]

    async with _core.open_nursery() as nursery:
        nursery.start_soon(cancellable_waiter, 1, lot1, scopes, record)
        await wait_all_tasks_blocked()
        nursery.start_soon(cancellable_waiter, 2, lot1, scopes, record)
        await wait_all_tasks_blocked()
        nursery.start_soon(cancellable_waiter, 3, lot1, scopes, record)
        await wait_all_tasks_blocked()
        assert len(record) == 3

        assert len(lot1) == 3
        lot1.repark(lot2)
        assert len(lot1) == 2
        assert len(lot2) == 1
        lot2.unpark_all()
        await wait_all_tasks_blocked()
        assert len(record) == 4
        assert record == ["sleep 1", "sleep 2", "sleep 3", "wake 1"]

        lot1.repark_all(lot2)
        assert len(lot1) == 0
        assert len(lot2) == 2

        scopes[2].cancel()
        await wait_all_tasks_blocked()
        assert len(lot2) == 1
        assert record == [
            "sleep 1",
            "sleep 2",
            "sleep 3",
            "wake 1",
            "cancelled 2",
        ]

        lot2.unpark_all()
        await wait_all_tasks_blocked()
        assert record == [
            "sleep 1",
            "sleep 2",
            "sleep 3",
            "wake 1",
            "cancelled 2",
            "wake 3",
        ]


async def test_parking_lot_repark_with_count() -> None:
    record: list[str] = []
    scopes: dict[int, _core.CancelScope] = {}
    lot1 = ParkingLot()
    lot2 = ParkingLot()
    async with _core.open_nursery() as nursery:
        nursery.start_soon(cancellable_waiter, 1, lot1, scopes, record)
        await wait_all_tasks_blocked()
        nursery.start_soon(cancellable_waiter, 2, lot1, scopes, record)
        await wait_all_tasks_blocked()
        nursery.start_soon(cancellable_waiter, 3, lot1, scopes, record)
        await wait_all_tasks_blocked()
        assert len(record) == 3

        assert len(lot1) == 3
        assert len(lot2) == 0
        lot1.repark(lot2, count=2)
        assert len(lot1) == 1
        assert len(lot2) == 2
        while lot2:
            lot2.unpark()
            await wait_all_tasks_blocked()
        assert record == [
            "sleep 1",
            "sleep 2",
            "sleep 3",
            "wake 1",
            "wake 2",
        ]
        lot1.unpark_all()


async def dummy_task(
    task_status: _core.TaskStatus[_core.Task] = trio.TASK_STATUS_IGNORED,
) -> None:
    task_status.started(_core.current_task())
    await trio.sleep_forever()


async def test_parking_lot_breaker_basic() -> None:
    lot = ParkingLot()
    task = current_task()

    with pytest.raises(
        RuntimeError,
        match="Attempted to remove task as breaker for a lot it is not registered for",
    ):
        remove_parking_lot_breaker(task, lot)

    # check that a task can be registered as breaker for the same lot multiple times
    add_parking_lot_breaker(task, lot)
    add_parking_lot_breaker(task, lot)
    remove_parking_lot_breaker(task, lot)
    remove_parking_lot_breaker(task, lot)

    with pytest.raises(
        RuntimeError,
        match="Attempted to remove task as breaker for a lot it is not registered for",
    ):
        remove_parking_lot_breaker(task, lot)

    # defaults to current task
    lot.break_lot()
    assert lot.broken_by == task

    # breaking the lot again with the same task is a no-op
    lot.break_lot()

    child_task = None
    with pytest.warns(RuntimeWarning):
        async with trio.open_nursery() as nursery:
            child_task = await nursery.start(dummy_task)
            # registering a task as breaker on an already broken lot is fine... though it
            # maybe shouldn't be as it will always cause a RuntimeWarning???
            # Or is this a sign that we shouldn't raise a warning?
            add_parking_lot_breaker(child_task, lot)
            nursery.cancel_scope.cancel()

    # manually breaking a lot with an already exited task is fine
    lot = ParkingLot()
    lot.break_lot(child_task)


async def test_parking_lot_breaker_warnings() -> None:
    lot = ParkingLot()
    task = current_task()
    lot.break_lot()

    warn_str = "attempted to break parking .* already broken by .*"
    # breaking an already broken lot with a different task gives a warning
    # The nursery is only to create a task we can pass to lot.break_lot
    async with trio.open_nursery() as nursery:
        child_task = await nursery.start(dummy_task)
        with pytest.warns(
            RuntimeWarning,
            match=warn_str,
        ):
            lot.break_lot(child_task)
        nursery.cancel_scope.cancel()

    # note that this get put into an exceptiongroup if inside a nursery, making any
    # stacklevel arguments irrelevant
    with RaisesGroup(Matcher(RuntimeWarning, match=warn_str)):
        async with trio.open_nursery() as nursery:
            child_task = await nursery.start(dummy_task)
            lot.break_lot(child_task)
            nursery.cancel_scope.cancel()

    # and doesn't change broken_by
    assert lot.broken_by == task

    # register multiple tasks as lot breakers, then have them all exit
    lot = ParkingLot()
    child_task = None
    # This does not give an exception group, as the warning is raised by the nursery
    # exiting, and not any of the tasks inside the nursery.
    # And we only get a single warning because... of the default warning filter? and the
    # location being the same? (because I haven't figured out why stacklevel makes no
    # difference)
    with pytest.warns(
        RuntimeWarning,
        match=warn_str,
    ):
        async with trio.open_nursery() as nursery:
            child_task = await nursery.start(dummy_task)
            child_task2 = await nursery.start(dummy_task)
            child_task3 = await nursery.start(dummy_task)
            add_parking_lot_breaker(child_task, lot)
            add_parking_lot_breaker(child_task2, lot)
            add_parking_lot_breaker(child_task3, lot)
            nursery.cancel_scope.cancel()

    # trying to register an exited task as lot breaker errors
    with pytest.raises(
        trio.BrokenResourceError,
        match="^Attempted to add already exited task as lot breaker.$",
    ):
        add_parking_lot_breaker(child_task, lot)


async def test_parking_lot_breaker() -> None:
    async def bad_parker(lot: ParkingLot, scope: _core.CancelScope) -> None:
        add_parking_lot_breaker(current_task(), lot)
        with scope:
            await trio.sleep_forever()

    lot = ParkingLot()
    cs = _core.CancelScope()

    # check that parked task errors
    with RaisesGroup(
        Matcher(_core.BrokenResourceError, match="^Parking lot broken by"),
    ):
        async with _core.open_nursery() as nursery:
            nursery.start_soon(bad_parker, lot, cs)
            await wait_all_tasks_blocked()

            nursery.start_soon(lot.park)
            await wait_all_tasks_blocked()

            cs.cancel()

    # check that trying to park in broken lot errors
    with pytest.raises(_core.BrokenResourceError):
        await lot.park()


async def test_parking_lot_weird() -> None:
    """Break a parking lot, where the breakee is parked.
    Doing this is weird, but should probably be supported.
    """

    async def return_me_and_park(
        lot: ParkingLot,
        *,
        task_status: _core.TaskStatus[_core.Task] = trio.TASK_STATUS_IGNORED,
    ) -> None:
        task_status.started(_core.current_task())
        await lot.park()

    lot = ParkingLot()
    with RaisesGroup(
        Matcher(_core.BrokenResourceError, match="^Parking lot broken by"),
    ):
        async with _core.open_nursery() as nursery:
            task = await nursery.start(return_me_and_park, lot)
            lot.break_lot(task)
