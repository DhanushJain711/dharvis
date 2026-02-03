"""Tests for the database module."""

import asyncio
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytz

from src.database import Database


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    db = Database(db_path)
    yield db

    # Cleanup
    if db_path.exists():
        os.unlink(db_path)


@pytest.fixture
def event_loop():
    """Create event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


class TestTaskOperations:
    """Tests for task CRUD operations."""

    @pytest.mark.asyncio
    async def test_init_db(self, temp_db):
        """Test database initialization creates tables."""
        await temp_db.init_db()
        # If no exception, tables were created

    @pytest.mark.asyncio
    async def test_add_task(self, temp_db):
        """Test adding a task."""
        await temp_db.init_db()

        task_id = await temp_db.add_task(
            title="Test Task",
            deadline="2024-01-20T12:00:00-06:00",
            priority="high",
            description="Test description",
        )

        assert task_id is not None
        assert task_id > 0

    @pytest.mark.asyncio
    async def test_get_task(self, temp_db):
        """Test getting a task by ID."""
        await temp_db.init_db()

        task_id = await temp_db.add_task(
            title="Test Task",
            deadline="2024-01-20T12:00:00-06:00",
        )

        task = await temp_db.get_task(task_id)

        assert task is not None
        assert task["title"] == "Test Task"
        assert task["status"] == "pending"

    @pytest.mark.asyncio
    async def test_get_pending_tasks(self, temp_db):
        """Test getting all pending tasks."""
        await temp_db.init_db()

        await temp_db.add_task(title="Task 1", deadline="2024-01-20T12:00:00-06:00")
        await temp_db.add_task(title="Task 2", deadline="2024-01-21T12:00:00-06:00")

        tasks = await temp_db.get_pending_tasks()

        assert len(tasks) == 2

    @pytest.mark.asyncio
    async def test_complete_task_by_id(self, temp_db):
        """Test completing a task by ID."""
        await temp_db.init_db()

        task_id = await temp_db.add_task(title="Test Task")

        success = await temp_db.complete_task(task_id=task_id)
        assert success

        task = await temp_db.get_task(task_id)
        assert task["status"] == "completed"
        assert task["completed_at"] is not None

    @pytest.mark.asyncio
    async def test_complete_task_by_title(self, temp_db):
        """Test completing a task by title."""
        await temp_db.init_db()

        await temp_db.add_task(title="Math Homework")

        success = await temp_db.complete_task(title="math homework")
        assert success

    @pytest.mark.asyncio
    async def test_delete_task(self, temp_db):
        """Test deleting a task."""
        await temp_db.init_db()

        task_id = await temp_db.add_task(title="Test Task")

        success = await temp_db.delete_task(task_id=task_id)
        assert success

        task = await temp_db.get_task(task_id)
        assert task is None

    @pytest.mark.asyncio
    async def test_update_task(self, temp_db):
        """Test updating task fields."""
        await temp_db.init_db()

        task_id = await temp_db.add_task(
            title="Original Title",
            priority="low",
        )

        success = await temp_db.update_task(
            task_id,
            title="Updated Title",
            priority="high",
        )
        assert success

        task = await temp_db.get_task(task_id)
        assert task["title"] == "Updated Title"
        assert task["priority"] == "high"

    @pytest.mark.asyncio
    async def test_fuzzy_match_task_exact(self, temp_db):
        """Test fuzzy matching with exact match."""
        await temp_db.init_db()

        await temp_db.add_task(title="Math Homework")

        task = await temp_db.fuzzy_match_task("math homework")
        assert task is not None
        assert task["title"] == "Math Homework"

    @pytest.mark.asyncio
    async def test_fuzzy_match_task_substring(self, temp_db):
        """Test fuzzy matching with substring match."""
        await temp_db.init_db()

        await temp_db.add_task(title="Math Homework Assignment")

        task = await temp_db.fuzzy_match_task("homework")
        assert task is not None
        assert "Homework" in task["title"]

    @pytest.mark.asyncio
    async def test_fuzzy_match_task_no_match(self, temp_db):
        """Test fuzzy matching with no match."""
        await temp_db.init_db()

        await temp_db.add_task(title="Math Homework")

        task = await temp_db.fuzzy_match_task("chemistry")
        assert task is None

    @pytest.mark.asyncio
    async def test_get_tasks_due_by(self, temp_db):
        """Test getting tasks due by a date."""
        await temp_db.init_db()

        await temp_db.add_task(title="Early Task", deadline="2024-01-15T12:00:00-06:00")
        await temp_db.add_task(title="Later Task", deadline="2024-01-25T12:00:00-06:00")

        tasks = await temp_db.get_tasks_due_by("2024-01-20T23:59:59-06:00")

        assert len(tasks) == 1
        assert tasks[0]["title"] == "Early Task"


class TestEventOperations:
    """Tests for event CRUD operations."""

    @pytest.mark.asyncio
    async def test_add_event(self, temp_db):
        """Test adding an event."""
        await temp_db.init_db()

        event_id = await temp_db.add_event(
            title="Team Meeting",
            start_time="2024-01-20T14:00:00-06:00",
            end_time="2024-01-20T15:00:00-06:00",
            location="Conference Room",
        )

        assert event_id is not None
        assert event_id > 0

    @pytest.mark.asyncio
    async def test_get_event(self, temp_db):
        """Test getting an event by ID."""
        await temp_db.init_db()

        event_id = await temp_db.add_event(
            title="Team Meeting",
            start_time="2024-01-20T14:00:00-06:00",
        )

        event = await temp_db.get_event(event_id)

        assert event is not None
        assert event["title"] == "Team Meeting"

    @pytest.mark.asyncio
    async def test_get_events_between(self, temp_db):
        """Test getting events in a date range."""
        await temp_db.init_db()

        await temp_db.add_event(
            title="Morning Meeting",
            start_time="2024-01-20T09:00:00-06:00",
        )
        await temp_db.add_event(
            title="Afternoon Meeting",
            start_time="2024-01-20T14:00:00-06:00",
        )
        await temp_db.add_event(
            title="Next Day Meeting",
            start_time="2024-01-21T10:00:00-06:00",
        )

        events = await temp_db.get_events_between(
            "2024-01-20T00:00:00-06:00",
            "2024-01-20T23:59:59-06:00",
        )

        assert len(events) == 2

    @pytest.mark.asyncio
    async def test_delete_event_by_id(self, temp_db):
        """Test deleting an event by ID."""
        await temp_db.init_db()

        event_id = await temp_db.add_event(
            title="Test Event",
            start_time="2024-01-20T14:00:00-06:00",
        )

        success = await temp_db.delete_event(event_id=event_id)
        assert success

        event = await temp_db.get_event(event_id)
        assert event is None

    @pytest.mark.asyncio
    async def test_delete_event_by_title(self, temp_db):
        """Test deleting an event by title."""
        await temp_db.init_db()

        await temp_db.add_event(
            title="Coffee Meeting",
            start_time="2024-01-20T14:00:00-06:00",
        )

        success = await temp_db.delete_event(title="coffee")
        assert success

    @pytest.mark.asyncio
    async def test_update_event(self, temp_db):
        """Test updating event fields."""
        await temp_db.init_db()

        event_id = await temp_db.add_event(
            title="Original Event",
            start_time="2024-01-20T14:00:00-06:00",
        )

        success = await temp_db.update_event(
            event_id,
            title="Updated Event",
            location="New Location",
        )
        assert success

        event = await temp_db.get_event(event_id)
        assert event["title"] == "Updated Event"
        assert event["location"] == "New Location"

    @pytest.mark.asyncio
    async def test_fuzzy_match_event(self, temp_db):
        """Test fuzzy matching for events."""
        await temp_db.init_db()

        await temp_db.add_event(
            title="Coffee with Jake",
            start_time="2024-01-20T14:00:00-06:00",
        )

        event = await temp_db.fuzzy_match_event("coffee")
        assert event is not None
        assert "Coffee" in event["title"]


class TestConversationContext:
    """Tests for conversation context operations."""

    @pytest.mark.asyncio
    async def test_add_conversation(self, temp_db):
        """Test adding a conversation entry."""
        await temp_db.init_db()

        conv_id = await temp_db.add_conversation(
            user_message="What do I have today?",
            bot_response="You have a meeting at 2pm.",
        )

        assert conv_id is not None

    @pytest.mark.asyncio
    async def test_get_recent_conversations(self, temp_db):
        """Test getting recent conversations."""
        await temp_db.init_db()

        await temp_db.add_conversation("Message 1", "Response 1")
        await temp_db.add_conversation("Message 2", "Response 2")
        await temp_db.add_conversation("Message 3", "Response 3")

        convs = await temp_db.get_recent_conversations(limit=2)

        assert len(convs) == 2
        # Should be in chronological order (oldest first)
        assert convs[0]["user_message"] == "Message 2"
        assert convs[1]["user_message"] == "Message 3"
