import datetime
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


class TestMaterialVisibility(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_dir_path = Path(self.temp_dir.name)
        self.hermes_root = self.temp_dir_path / "hermes"
        self.hermes_root.mkdir(parents=True)
        self.profiles_root = self.hermes_root / "profiles"
        self.profiles_root.mkdir(parents=True)
        self.profile_name = "test_profile"
        self.home = self.profiles_root / self.profile_name
        self.home.mkdir(parents=True)
        self.state_db = self.home / "state.db"
        day_start = datetime.datetime.strptime(
            "2023-01-01 09:00", "%Y-%m-%d %H:%M"
        ).timestamp()

        with sqlite3.connect(self.state_db) as conn:
            conn.execute("DROP TABLE IF EXISTS messages")
            conn.execute("""
                CREATE TABLE messages (
                    session_id TEXT,
                    role TEXT,
                    content TEXT,
                    tool_name TEXT,
                    tool_call_id TEXT,
                    tool_calls TEXT,
                    timestamp REAL
                )
            """)
            conn.execute(
                "INSERT INTO messages (session_id, role, content, tool_name, tool_call_id, tool_calls, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "sess1",
                    "assistant",
                    None,
                    None,
                    None,
                    json.dumps(
                        [
                            {
                                "id": "call1",
                                "type": "function",
                                "function": {
                                    "name": "terminal",
                                    "arguments": json.dumps(
                                        {"command": "echo test > file.txt"}
                                    ),
                                },
                            }
                        ]
                    ),
                    day_start,
                ),
            )
            conn.execute(
                "INSERT INTO messages (session_id, role, content, tool_name, tool_call_id, tool_calls, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "sess1",
                    "tool",
                    json.dumps({"output": "ok", "exit_code": 0}),
                    "terminal",
                    "call1",
                    None,
                    day_start + 1,
                ),
            )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_terminal_command_visibility(self):
        from main import collect_material
        result = collect_material(day="2023-01-01", home=self.home)
        material = result["material"]
        self.assertIn("## 成功した操作", material)
        self.assertIn("echo test > file.txt", material)

    def test_terminal_without_zero_exit_code_is_not_reported_as_success(self):
        timestamp = datetime.datetime.strptime(
            "2023-01-01 09:05", "%Y-%m-%d %H:%M"
        ).timestamp()
        tool_calls = json.dumps(
            [
                {
                    "id": "call2",
                    "type": "function",
                    "function": {
                        "name": "terminal",
                        "arguments": json.dumps({"command": "cat uncertain.txt"}),
                    },
                }
            ]
        )
        with sqlite3.connect(self.state_db) as conn:
            conn.execute(
                "INSERT INTO messages "
                "(session_id, role, content, tool_name, tool_call_id, tool_calls, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("sess1", "assistant", None, None, None, tool_calls, timestamp),
            )
            conn.execute(
                "INSERT INTO messages "
                "(session_id, role, content, tool_name, tool_call_id, tool_calls, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "sess1",
                    "tool",
                    json.dumps({"output": "result without exit status"}),
                    "terminal",
                    "call2",
                    None,
                    timestamp + 1,
                ),
            )

        from main import collect_material
        material = collect_material(day="2023-01-01", home=self.home)["material"]
        success_section = material.split("## 成功した操作\n", 1)[1].split("\n## ", 1)[0]
        self.assertNotIn("cat uncertain.txt", success_section)

if __name__ == "__main__":
    unittest.main()
