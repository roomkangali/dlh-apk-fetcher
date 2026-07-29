#!/usr/bin/env python3
"""Droid LLM Hunter APK Fetcher"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

APP_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = APP_DIR / "downloads"
LOGS_DIR = APP_DIR / "logs"
LOG_FILE = LOGS_DIR / "app.log"

console = Console()


class APKDownloaderError(Exception):
    """Base exception for the APK downloader application."""


class ADBNotFoundError(APKDownloaderError):
    """Raised when ADB is not installed or not available in PATH."""


class DeviceError(APKDownloaderError):
    """Raised when there is a device related error."""


class PackageError(APKDownloaderError):
    """Raised when package lookup or selection fails."""


class DownloadError(APKDownloaderError):
    """Raised when APK download fails."""


@dataclass(slots=True)
class Device:
    """Represents a connected Android device."""

    serial: str
    state: str


@dataclass(slots=True)
class APKEntry:
    """Represents an APK file path on the device."""

    remote_path: str
    file_name: str

    @property
    def display_name(self) -> str:
        """Return the base filename for display purposes."""
        return Path(self.remote_path).name


def setup_directories() -> None:
    """Create required application directories."""
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def setup_logging() -> None:
    """Configure file logging for the application."""
    setup_directories()
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


class ADBManager:
    """Handles all low-level ADB command execution."""

    def __init__(self) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)

    def check_adb_installed(self) -> None:
        """Ensure ADB is available on the system."""
        self.logger.info("Checking ADB installation")
        if shutil.which("adb") is None:
            raise ADBNotFoundError(
                "ADB not found.\nPlease install Android Platform Tools first."
            )

        try:
            self.run_command(["adb", "version"])
        except APKDownloaderError as exc:
            raise ADBNotFoundError(
                "ADB not found.\nPlease install Android Platform Tools first."
            ) from exc

    def run_command(
        self,
        command: Sequence[str],
        *,
        device_serial: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Execute a command and return the completed process."""
        full_command = list(command)
        if device_serial and command[:1] == ["adb"]:
            full_command = ["adb", "-s", device_serial, *command[1:]]

        self.logger.info("Running command: %s", " ".join(full_command))

        try:
            result = subprocess.run(
                full_command,
                capture_output=True,
                text=True,
                check=False,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:
            raise ADBNotFoundError(
                "ADB not found.\nPlease install Android Platform Tools first."
            ) from exc
        except OSError as exc:
            raise APKDownloaderError(f"Failed to run command: {exc}") from exc

        if check and result.returncode != 0:
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            message = stderr or stdout or "ADB command failed."
            self.logger.error("Command failed: %s", message)
            raise APKDownloaderError(message)

        return result


class DeviceManager:
    """Handles device detection and selection."""

    def __init__(self, adb_manager: ADBManager) -> None:
        self.adb_manager = adb_manager
        self.logger = logging.getLogger(self.__class__.__name__)
        self._selected_device: Device | None = None

    @property
    def selected_device(self) -> Device | None:
        """Return the currently selected device."""
        return self._selected_device

    def refresh_devices(self) -> list[Device]:
        """Retrieve the list of connected devices."""
        result = self.adb_manager.run_command(["adb", "devices"])
        devices: list[Device] = []

        for line in result.stdout.splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            devices.append(Device(serial=parts[0], state=parts[1]))

        self.logger.info("Detected %s device(s)", len(devices))
        return devices

    def ensure_device_selected(self) -> Device:
        """Ensure a valid device is selected."""
        devices = self.refresh_devices()
        if not devices:
            raise DeviceError("No connected device/emulator was found.")

        offline_devices = [device for device in devices if device.state != "device"]
        online_devices = [device for device in devices if device.state == "device"]

        if offline_devices and not online_devices:
            offline_text = ", ".join(device.serial for device in offline_devices)
            raise DeviceError(f"Offline/unauthorized device detected: {offline_text}")

        if not online_devices:
            raise DeviceError("No device/emulator is ready to use.")

        if len(online_devices) == 1:
            self._selected_device = online_devices[0]
            return online_devices[0]

        table = Table(title="Device List", header_style="bold cyan")
        table.add_column("No", style="bold")
        table.add_column("Serial", style="green")
        table.add_column("State", style="yellow")

        for index, device in enumerate(online_devices, start=1):
            table.add_row(str(index), device.serial, device.state)

        console.print(table)

        while True:
            choice = click.prompt("Select device", type=str).strip()
            if choice.isdigit():
                index = int(choice) - 1
                if 0 <= index < len(online_devices):
                    self._selected_device = online_devices[index]
                    return online_devices[index]

            for device in online_devices:
                if choice == device.serial:
                    self._selected_device = device
                    return device

            console.print("[red]Invalid device selection.[/red]")

    def get_selected_serial(self) -> str:
        """Return the selected device serial."""
        device = self._selected_device or self.ensure_device_selected()
        return device.serial


class PackageManager:
    """Handles package listing, searching, and APK path lookup."""

    def __init__(self, adb_manager: ADBManager, device_manager: DeviceManager) -> None:
        self.adb_manager = adb_manager
        self.device_manager = device_manager
        self.logger = logging.getLogger(self.__class__.__name__)

    def list_packages(self) -> list[str]:
        """List installed packages on the selected device."""
        serial = self.device_manager.get_selected_serial()
        result = self.adb_manager.run_command(
            ["adb", "shell", "pm", "list", "packages"],
            device_serial=serial,
        )

        packages = sorted(
            line.replace("package:", "").strip()
            for line in result.stdout.splitlines()
            if line.strip().startswith("package:")
        )

        self.logger.info("Loaded %s package(s)", len(packages))
        return packages

    def list_running_packages(self) -> list[str]:
        """List installed packages that currently have a running process."""
        serial = self.device_manager.get_selected_serial()
        installed_packages = self.list_packages()

        commands_to_try = [
            ["adb", "shell", "ps"],
            ["adb", "shell", "ps", "-A"],
        ]

        process_names: set[str] = set()
        for command in commands_to_try:
            try:
                result = self.adb_manager.run_command(command, device_serial=serial)
            except APKDownloaderError:
                continue

            for line in result.stdout.splitlines()[1:]:
                line = line.strip()
                if not line:
                    continue
                parts = line.split()
                if parts:
                    process_names.add(parts[-1])

            if process_names:
                break

        running_packages: set[str] = set()
        installed_package_set = set(installed_packages)

        for process_name in process_names:
            candidates = [process_name]
            if ":" in process_name:
                candidates.append(process_name.split(":", 1)[0])

            for candidate in candidates:
                if candidate in installed_package_set:
                    running_packages.add(candidate)

        packages = sorted(running_packages)
        self.logger.info("Loaded %s running package(s)", len(packages))
        return packages

    def search_packages(self, query: str, packages: Iterable[str] | None = None) -> list[str]:
        """Search packages by substring match."""
        package_list = list(packages) if packages is not None else self.list_packages()
        query_normalized = query.strip().lower()
        if not query_normalized:
            return package_list
        return [package for package in package_list if query_normalized in package.lower()]

    def display_packages(self, packages: Sequence[str], title: str = "Installed Applications") -> None:
        """Display packages in a rich table."""
        table = Table(title=title, header_style="bold magenta")
        table.add_column("No", style="bold", width=6)
        table.add_column("Package Name", style="green")

        for index, package in enumerate(packages, start=1):
            table.add_row(str(index), package)

        console.print(table)

    def select_package(self, packages: Sequence[str], user_input: str) -> str:
        """Select a package by index or exact package name."""
        candidate = user_input.strip()
        if not candidate:
            raise PackageError("Package input cannot be empty.")

        if candidate.isdigit():
            index = int(candidate) - 1
            if 0 <= index < len(packages):
                return packages[index]
            raise PackageError("Invalid package number.")

        if candidate in packages:
            return candidate

        raise PackageError("Package not found.")

    def get_apk_paths(self, package_name: str) -> list[APKEntry]:
        """Retrieve all APK paths for the selected package."""
        serial = self.device_manager.get_selected_serial()
        result = self.adb_manager.run_command(
            ["adb", "shell", "pm", "path", package_name],
            device_serial=serial,
        )

        entries: list[APKEntry] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line.startswith("package:"):
                continue
            remote_path = line.replace("package:", "", 1).strip()
            remote_name = Path(remote_path).name
            safe_package = package_name.replace(".", "_")
            file_name = f"{safe_package}_{remote_name}"
            entries.append(APKEntry(remote_path=remote_path, file_name=file_name))

        if not entries:
            raise PackageError("Package not found or APK paths could not be retrieved.")

        self.logger.info("Found %s APK file(s) for %s", len(entries), package_name)
        return entries

    def export_packages(self, packages: Sequence[str], output_file: Path) -> Path:
        """Export package list to a text file."""
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text("\n".join(packages) + "\n", encoding="utf-8")
        self.logger.info("Exported package list to %s", output_file)
        return output_file


class Downloader:
    """Downloads APK files from the selected device."""

    def __init__(self, adb_manager: ADBManager, device_manager: DeviceManager) -> None:
        self.adb_manager = adb_manager
        self.device_manager = device_manager
        self.logger = logging.getLogger(self.__class__.__name__)

    def download_apks(self, package_name: str, apks: Sequence[APKEntry]) -> list[Path]:
        """Download all APKs for a package."""
        serial = self.device_manager.get_selected_serial()
        setup_directories()
        package_download_dir = DOWNLOADS_DIR / package_name
        package_download_dir.mkdir(parents=True, exist_ok=True)
        downloaded_files: list[Path] = []

        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            console=console,
        )

        with progress:
            task_id = progress.add_task("Downloading APK...", total=len(apks))
            for apk in apks:
                progress.update(task_id, description=f"Downloading {apk.display_name}...")
                destination = package_download_dir / apk.file_name

                try:
                    self.adb_manager.run_command(
                        ["adb", "pull", apk.remote_path, os.fspath(destination)],
                        device_serial=serial,
                    )
                except APKDownloaderError as exc:
                    raise DownloadError(f"Failed to download {apk.display_name}: {exc}") from exc

                downloaded_files.append(destination)
                self.logger.info("Downloaded %s to %s", apk.remote_path, destination)
                progress.advance(task_id)

        return downloaded_files


class CLI:
    """Interactive CLI application controller."""

    def __init__(self) -> None:
        setup_logging()
        setup_directories()
        self.adb_manager = ADBManager()
        self.device_manager = DeviceManager(self.adb_manager)
        self.package_manager = PackageManager(self.adb_manager, self.device_manager)
        self.downloader = Downloader(self.adb_manager, self.device_manager)
        self.logger = logging.getLogger(self.__class__.__name__)
        self.cached_packages: list[str] = []

    def run(self) -> None:
        """Run the interactive CLI application."""
        try:
            self.initialize()
            self.main_menu()
        except KeyboardInterrupt:
            console.print("\n[yellow]Cancelled by user.[/yellow]")
            self.logger.warning("Application interrupted by user")
        except APKDownloaderError as exc:
            console.print(f"[red]{exc}[/red]")
            self.logger.exception("Application error: %s", exc)
            sys.exit(1)

    def load_packages(self) -> list[str]:
        """Load all installed packages."""
        self.cached_packages = self.package_manager.list_packages()
        return self.cached_packages

    def load_running_packages(self) -> list[str]:
        """Load only running packages."""
        self.cached_packages = self.package_manager.list_running_packages()
        return self.cached_packages

    def initialize(self) -> None:
        """Initialize ADB and device state."""
        self.adb_manager.check_adb_installed()
        device = self.device_manager.ensure_device_selected()
        self.load_packages()
        console.print(
            Panel.fit(
                (
                    "[bold cyan]APK Downloader via ADB[/bold cyan]\n\n"
                    f"Device: [green]{device.serial}[/green]\n"
                    f"Total apps: [yellow]{len(self.cached_packages)}[/yellow]"
                ),
                border_style="cyan",
            )
        )

    def refresh(self) -> None:
        """Refresh device and package data."""
        device = self.device_manager.ensure_device_selected()
        self.load_packages()
        console.print(
            f"[green]Active device:[/green] {device.serial} | "
            f"[green]Total apps:[/green] {len(self.cached_packages)}"
        )

    def main_menu(self) -> None:
        """Display and process the main menu."""
        while True:
            console.print(
                Panel.fit(
                    "1. List applications\n"
                    "2. List running applications\n"
                    "3. Search applications\n"
                    "4. Download APK\n"
                    "5. Refresh device\n"
                    "6. Export package list\n"
                    "7. Exit",
                    title="APK Downloader",
                    border_style="blue",
                )
            )

            choice = click.prompt("Select menu", type=click.Choice(["1", "2", "3", "4", "5", "6", "7"]))

            if choice == "1":
                self.handle_list_packages()
            elif choice == "2":
                self.handle_list_running_packages()
            elif choice == "3":
                self.handle_search_packages()
            elif choice == "4":
                self.handle_download()
            elif choice == "5":
                self.refresh()
            elif choice == "6":
                self.handle_export_packages()
            elif choice == "7":
                console.print("[bold green]Done.[/bold green]")
                break

    def handle_list_packages(self) -> None:
        """Display all installed packages."""
        self.load_packages()
        self.package_manager.display_packages(
            self.cached_packages,
            title=f"Installed Applications ({len(self.cached_packages)})",
        )

    def handle_list_running_packages(self) -> None:
        """Display only running applications."""
        self.load_running_packages()
        self.package_manager.display_packages(
            self.cached_packages,
            title=f"Running Applications ({len(self.cached_packages)})",
        )

    def handle_search_packages(self) -> None:
        """Search for packages and display the result."""
        query = click.prompt("Search package", type=str, default="", show_default=False)
        self.load_packages()
        results = self.package_manager.search_packages(query, self.cached_packages)

        if not results:
            console.print("[yellow]No matching package found.[/yellow]")
            return

        self.package_manager.display_packages(results, title=f"Search Results ({len(results)})")

    def handle_download(self) -> None:
        """Handle APK selection and download flow."""
        self.load_packages()
        query = click.prompt(
            "Search package (leave blank to show all)",
            type=str,
            default="",
            show_default=False,
        )
        visible_packages = self.package_manager.search_packages(query, self.cached_packages)

        if not visible_packages:
            console.print("[yellow]No matching package found.[/yellow]")
            return

        self.package_manager.display_packages(visible_packages, title="Select Application")
        selection = click.prompt("Enter number or package name", type=str)

        package_name = self.package_manager.select_package(visible_packages, selection)
        apks = self.package_manager.get_apk_paths(package_name)
        downloaded_files = self.downloader.download_apks(package_name, apks)

        console.print("[bold green]✔ Download completed[/bold green]")
        console.print(f"[cyan]Package folder:[/cyan] {downloaded_files[0].parent}")
        console.print("[cyan]File location:[/cyan]")
        for file_path in downloaded_files:
            console.print(f" - {file_path}")

    def handle_export_packages(self) -> None:
        """Export package list to a text file."""
        self.load_packages()
        output_path = APP_DIR / "downloads" / "packages.txt"
        saved_file = self.package_manager.export_packages(self.cached_packages, output_path)
        console.print(f"[green]Package list saved to:[/green] {saved_file}")


@click.command()
def main() -> None:
    """Entrypoint for the APK Downloader CLI."""
    cli = CLI()
    cli.run()


if __name__ == "__main__":
    main()