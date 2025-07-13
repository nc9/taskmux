"""
Daemon mode for Taskmux with enhanced monitoring and WebSocket API.
"""

import asyncio
import json
import logging
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Set

import websockets
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


class ConfigWatcher(FileSystemEventHandler):
    """File system event handler for monitoring config file changes."""

    def __init__(self, taskmux_cli, daemon_mode=False):
        self.taskmux_cli = taskmux_cli
        self.daemon_mode = daemon_mode

    def on_modified(self, event):
        if event.src_path.endswith("taskmux.json"):
            print("\n🔄 Config file changed, reloading...")
            self.taskmux_cli.config.load_config()

            # In daemon mode, restart affected tasks
            if self.daemon_mode:
                self.taskmux_cli.handle_config_reload()


class TaskmuxDaemon:
    """Daemon mode for Taskmux with enhanced monitoring and API"""

    def __init__(self, config_path: str = "taskmux.json", api_port: int = 8765):
        self.config_path = config_path
        self.api_port = api_port
        self.running = False
        self.cli = None
        self.observer = None
        self.health_check_interval = 30  # seconds
        self.health_check_task = None
        self.websocket_clients: Set = set()
        self.logger = self._setup_logging()

        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _setup_logging(self) -> logging.Logger:
        """Setup logging for daemon mode"""
        logger = logging.getLogger("taskmux-daemon")
        logger.setLevel(logging.INFO)

        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        # File handler
        log_file = Path.home() / ".taskmux" / "daemon.log"
        log_file.parent.mkdir(exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        return logger

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        self.logger.info(f"Received signal {signum}, shutting down...")
        self.stop()
        sys.exit(0)

    async def start(self):
        """Start daemon mode with monitoring and API"""
        self.running = True

        # Import here to avoid circular import
        from .cli import TaskmuxCLI

        self.cli = TaskmuxCLI()

        self.logger.info(f"Starting Taskmux daemon for config: {self.config_path}")

        # Start file watching
        self.observer = Observer()
        self.observer.schedule(
            ConfigWatcher(self.cli, daemon_mode=True), ".", recursive=False
        )
        self.observer.start()
        self.logger.info("Started config file watcher")

        # Start health checking
        self.health_check_task = asyncio.create_task(self._health_check_loop())

        # Start WebSocket API server
        api_task = asyncio.create_task(self._start_api_server())

        self.logger.info(f"Taskmux daemon started on port {self.api_port}")
        self.logger.info("Use Ctrl+C to stop")

        try:
            await asyncio.gather(self.health_check_task, api_task)
        except asyncio.CancelledError:
            self.logger.info("Daemon tasks cancelled")

    def stop(self):
        """Stop daemon mode"""
        self.running = False

        if self.observer:
            self.observer.stop()
            self.observer.join()

        if self.health_check_task and not self.health_check_task.done():
            self.health_check_task.cancel()

        self.logger.info("Taskmux daemon stopped")

    async def _health_check_loop(self):
        """Continuous health checking loop"""
        while self.running:
            try:
                if self.cli and self.cli.tmux.session_exists():
                    self.cli.tmux.auto_restart_unhealthy_tasks()

                    # Broadcast health status to WebSocket clients
                    if self.websocket_clients:
                        status = await self._get_full_status()
                        await self._broadcast_to_clients(
                            {"type": "health_check", "data": status}
                        )

                await asyncio.sleep(self.health_check_interval)
            except Exception as e:
                self.logger.error(f"Health check error: {e}")
                await asyncio.sleep(5)  # Short sleep on error

    async def _start_api_server(self):
        """Start WebSocket API server"""

        async def handle_client(websocket, path):
            self.websocket_clients.add(websocket)
            self.logger.info(
                f"New WebSocket client connected: {websocket.remote_address}"
            )

            try:
                async for message in websocket:
                    try:
                        data = json.loads(message)
                        response = await self._handle_api_request(data)
                        await websocket.send(json.dumps(response))
                    except json.JSONDecodeError:
                        await websocket.send(json.dumps({"error": "Invalid JSON"}))
                    except Exception as e:
                        await websocket.send(json.dumps({"error": str(e)}))
            except websockets.exceptions.ConnectionClosed:
                pass
            finally:
                self.websocket_clients.discard(websocket)
                self.logger.info(
                    f"WebSocket client disconnected: {websocket.remote_address}"
                )

        start_server = websockets.serve(handle_client, "localhost", self.api_port)
        await start_server

    async def _handle_api_request(self, data: dict) -> dict:
        """Handle WebSocket API requests"""
        command = data.get("command")
        params = data.get("params", {})

        if command == "status":
            return await self._get_full_status()
        elif command == "restart":
            task_name = params.get("task")
            if task_name:
                self.cli.tmux.restart_task(task_name)
                return {"success": True, "message": f"Restarted {task_name}"}
            return {"error": "Task name required"}
        elif command == "kill":
            task_name = params.get("task")
            if task_name:
                self.cli.tmux.kill_task(task_name)
                return {"success": True, "message": f"Killed {task_name}"}
            return {"error": "Task name required"}
        elif command == "logs":
            task_name = params.get("task")
            lines = params.get("lines", 100)
            if task_name and self.cli.tmux.session_exists():
                # Get logs via libtmux
                try:
                    window = self.cli.tmux.session.windows.get(window_name=task_name)
                    if window:
                        pane = window.active_pane
                        output = pane.cmd(
                            "capture-pane", "-p", "-S", f"-{lines}"
                        ).stdout
                        return {"success": True, "logs": output}
                except Exception:
                    pass
            return {"error": "Could not retrieve logs"}
        else:
            return {"error": f"Unknown command: {command}"}

    async def _get_full_status(self) -> dict:
        """Get comprehensive status information"""
        if not self.cli:
            return {"error": "CLI not initialized"}

        session_exists = self.cli.tmux.session_exists()
        tasks_status = {}

        for task_name in self.cli.config.tasks:
            tasks_status[task_name] = self.cli.tmux.get_task_status(task_name)

        return {
            "session_name": self.cli.config.session_name,
            "session_exists": session_exists,
            "tasks": tasks_status,
            "api_type": "libtmux",
            "timestamp": datetime.now().isoformat(),
        }

    async def _broadcast_to_clients(self, message: dict):
        """Broadcast message to all connected WebSocket clients"""
        if not self.websocket_clients:
            return

        message_str = json.dumps(message)
        disconnected = set()

        for client in self.websocket_clients:
            try:
                await client.send(message_str)
            except:
                disconnected.add(client)

        # Remove disconnected clients
        self.websocket_clients -= disconnected


class SimpleConfigWatcher:
    """Simple config file watcher for non-daemon mode."""

    def __init__(self, taskmux_cli):
        self.taskmux_cli = taskmux_cli

    def watch_config(self):
        """Watch config file for changes"""
        print(f"👀 Watching {self.taskmux_cli.config.config_path} for changes...")
        print("Press Ctrl+C to stop")

        observer = Observer()
        observer.schedule(ConfigWatcher(self.taskmux_cli), ".", recursive=False)
        observer.start()

        try:
            import time

            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            observer.stop()
            print("\n👋 Stopped watching")

        observer.join()
