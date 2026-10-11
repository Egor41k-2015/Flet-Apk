import asyncio
import re
import subprocess

import flet as ft


# ────────────────────────────────────────────────────────────
#  Работа с Android shell
# ────────────────────────────────────────────────────────────

def shell(cmd: list[str], timeout: int = 10) -> tuple[str, str]:
    """Запускает shell-команду Android. Возвращает (stdout, stderr)."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
        return proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return "", "⏱ Команда не ответила за отведённое время"
    except FileNotFoundError as ex:
        return "", f"❌ Команда не найдена: {ex}"
    except Exception as ex:
        return "", f"❌ {type(ex).__name__}: {ex}"


def list_packages(include_system: bool = True) -> list[str]:
    """Возвращает список имён установленных пакетов."""
    cmd = ["pm", "list", "packages"]
    if not include_system:
        cmd.append("-3")  # только сторонние приложения
    out, err = shell(cmd)
    if err and not out:
        return []
    packages = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            packages.append(line[len("package:"):])
    return sorted(set(packages))


def package_details(package: str) -> dict:
    """Возвращает подробности о пакете через dumpsys."""
    out, err = shell(["dumpsys", "package", package], timeout=15)
    info = {
        "package": package,
        "version_name": "—",
        "version_code": "—",
        "min_sdk": "—",
        "target_sdk": "—",
        "installer": "—",
        "first_install": "—",
        "last_update": "—",
        "permissions": [],
        "activities": [],
        "raw_ok": bool(out),
        "error": err if not out else "",
    }

    if not out:
        return info

    m = re.search(r"versionName=([^\s]+)", out)
    if m:
        info["version_name"] = m.group(1)

    m = re.search(r"versionCode=(\d+)", out)
    if m:
        info["version_code"] = m.group(1)

    m = re.search(r"minSdk=(\d+)", out)
    if m:
        info["min_sdk"] = m.group(1)

    m = re.search(r"targetSdk=(\d+)", out)
    if m:
        info["target_sdk"] = m.group(1)

    m = re.search(r"installerPackageName=([^\s]+)", out)
    if m:
        info["installer"] = m.group(1)

    m = re.search(r"firstInstallTime=([^\n]+)", out)
    if m:
        info["first_install"] = m.group(1).strip()

    m = re.search(r"lastUpdateTime=([^\n]+)", out)
    if m:
        info["last_update"] = m.group(1).strip()

    for m in re.finditer(r"(android\.permission\.[A-Z_]+): granted=true", out):
        info["permissions"].append(m.group(1).split(".")[-1])

    for m in re.finditer(r"([\w.]+/[\w.$]+)\s+filter", out):
        info["activities"].append(m.group(1))

    if not info["activities"]:
        for m in re.finditer(r"([\w.]+)/([\w.$]+)\s*$", out, re.MULTILINE):
            info["activities"].append(f"{m.group(1)}/{m.group(2)}")

    return info


def launch_package(package: str) -> tuple[bool, str]:
    """Пытается запустить приложение через monkey или am start."""
    out, err = shell(["monkey", "-p", package, "-c",
                      "android.intent.category.LAUNCHER", "1"])
    if "Events injected: 1" in out:
        return True, "Приложение запущено"

    out2, _ = shell(["cmd", "package", "resolve-activity",
                     "--brief", package])
    for line in out2.splitlines():
        line = line.strip()
        if "/" in line and not line.startswith("priority"):
            parts = line.split("/")
            if len(parts) == 2:
                component = f"{parts[0]}/{parts[1]}"
                out3, err3 = shell(["am", "start", "-n", component])
                if "Error" not in out3 and "Exception" not in out3:
                    return True, f"Запущено через {component}"

    return False, err or "Не удалось определить launcher activity"


# ────────────────────────────────────────────────────────────
#  Приложение
# ────────────────────────────────────────────────────────────

class PackageViewerApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.all_packages: list[str] = []
        self.filtered: list[str] = []
        self.selected: str | None = None

        self._setup_theme()
        self._build_ui()
        self.page.run_task(self._load_packages)

    def _setup_theme(self):
        self.page.title = "Пакеты Android"
        self.page.padding = 0
        self.page.window.width = 480
        self.page.window.height = 820
        self.page.theme = ft.Theme(color_scheme_seed=ft.Colors.TEAL)
        self.page.dark_theme = ft.Theme(color_scheme_seed=ft.Colors.TEAL)
        self.page.theme_mode = ft.ThemeMode.SYSTEM

    def _build_ui(self):
        # Поле поиска
        self.search = ft.TextField(
            hint_text="Поиск по имени пакета…",
            prefix_icon=ft.Icons.SEARCH,
            dense=True,
            border=ft.OutlineInputBorder(
                side=ft.BorderSide(1, ft.Colors.GREY_400)),
            on_change=self._on_search,
        )

        # Переключатель системных
        self.system_switch = ft.Switch(
            label="Показывать системные",
            value=False,
            on_change=self._on_toggle_system,
        )

        # Список пакетов
        self.list_view = ft.ListView(
            spacing=2, padding=4, expand=True,
        )

        # Счётчик
        self.counter = ft.Text("", size=12, color=ft.Colors.GREY_600)

        # Индикатор загрузки — стандартный Material 3
        self.loading = ft.ProgressRing(
            width=20, height=20, stroke_width=2.5,
            visible=False,
        )

        # Статус
        self.status = ft.Text("Загрузка списка…", size=13)

        # ── Правая панель (детали) ──
        self.detail_title = ft.Text("Выберите пакет", size=18,
                                    weight=ft.FontWeight.BOLD)
        self.detail_pkg = ft.Text("", size=12, color=ft.Colors.GREY_600,
                                  selectable=True)
        self.detail_info = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO,
                                     expand=True)
        self.launch_btn = ft.FilledButton(
            "Запустить приложение",
            icon=ft.Icons.PLAY_ARROW,
            on_click=self._on_launch,
            visible=False,
        )
        self.refresh_detail_btn = ft.OutlinedButton(
            "Обновить сведения",
            icon=ft.Icons.REFRESH,
            on_click=self._on_refresh_detail,
            visible=False,
        )

        # ── Layout ──
        list_panel = ft.Container(
            width=280,
            padding=8,
            content=ft.Column([
                ft.Row([
                    ft.Icon(ft.Icons.ANDROID, color=ft.Colors.PRIMARY),
                    ft.Text("Пакеты", size=18, weight=ft.FontWeight.BOLD),
                    ft.Container(expand=True),
                    ft.IconButton(ft.Icons.REFRESH,
                                  tooltip="Перезагрузить список",
                                  on_click=self._on_reload),
                ]),
                self.search,
                ft.Row([
                    self.system_switch,
                    ft.Container(expand=True),
                    self.counter,
                ]),
                ft.Divider(height=8),
                ft.Container(self.list_view, expand=True),
                ft.Row([self.loading, self.status],
                       alignment=ft.MainAxisAlignment.CENTER),
            ], spacing=6, expand=True),
            bgcolor=ft.Colors.GREY_100,
        )

        detail_panel = ft.Container(
            expand=True,
            padding=16,
            content=ft.Column([
                self.detail_title,
                self.detail_pkg,
                ft.Divider(height=10),
                ft.Container(self.detail_info, expand=True),
                ft.Row([self.launch_btn, self.refresh_detail_btn],
                       spacing=8),
            ], spacing=6, expand=True),
        )

        self.page.add(
            ft.Row([list_panel, ft.VerticalDivider(width=1),
                    detail_panel], spacing=0, expand=True)
        )

    # ─── Список пакетов ───

    async def _load_packages(self):
        self.loading.visible = True
        self.status.value = "Сканирую систему…"
        self.page.update()

        loop = asyncio.get_event_loop()
        packages = await loop.run_in_executor(
            None, lambda: list_packages(include_system=True))

        self.all_packages = packages
        self._apply_filter()
        self.loading.visible = False
        self.status.value = f"Найдено: {len(packages)}"
        self.page.update()

    def _apply_filter(self):
        query = (self.search.value or "").strip().lower()
        show_sys = self.system_switch.value

        def matches(pkg: str) -> bool:
            is_system = (
                pkg.startswith("com.android.")
                or pkg.startswith("com.google.")
                or pkg.startswith("android.")
            )
            if not show_sys and is_system:
                return False
            if query and query not in pkg.lower():
                return False
            return True

        self.filtered = [p for p in self.all_packages if matches(p)]
        self._render_list()

    def _render_list(self):
        self.list_view.controls.clear()

        if not self.filtered:
            self.list_view.controls.append(
                ft.Container(
                    ft.Text("Ничего не найдено",
                            color=ft.Colors.GREY_500, size=13),
                    padding=20, alignment=ft.alignment.center,
                )
            )
        else:
            for pkg in self.filtered[:500]:
                self.list_view.controls.append(
                    ft.Container(
                        content=ft.Text(pkg, size=13, selectable=True),
                        padding=ft.Padding.symmetric(
                            vertical=6, horizontal=10),
                        bgcolor=(ft.Colors.PRIMARY_CONTAINER
                                 if pkg == self.selected
                                 else ft.Colors.WHITE),
                        border_radius=6,
                        ink=True,
                        on_click=lambda e, p=pkg: self.page.run_task(
                            self._select_package, p),
                    )
                )
        self.counter.value = f"{len(self.filtered)}"
        self.page.update()

    def _on_search(self, e):
        self._apply_filter()

    def _on_toggle_system(self, e):
        self._apply_filter()

    async def _on_reload(self, e):
        self.search.value = ""
        self.selected = None
        await self._load_packages()

    # ─── Детали пакета ───

    async def _select_package(self, pkg: str):
        self.selected = pkg
        self._render_list()

        self.detail_title.value = pkg.split(".")[-1].capitalize()
        self.detail_pkg.value = pkg
        self.detail_info.controls.clear()
        self.detail_info.controls.append(
            ft.Text("Загрузка сведений…", color=ft.Colors.GREY_600)
        )
        self.launch_btn.visible = False
        self.refresh_detail_btn.visible = True
        self.page.update()

        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(
            None, lambda: package_details(pkg))

        self._render_details(info)

    async def _on_refresh_detail(self, e):
        if self.selected:
            await self._select_package(self.selected)

    def _render_details(self, info: dict):
        self.detail_info.controls.clear()

        if not info["raw_ok"]:
            self.detail_info.controls.append(
                ft.Text(
                    f"❌ Не удалось получить сведения.\n{info['error']}",
                    color=ft.Colors.RED_600, size=13,
                )
            )
            self.page.update()
            return

        def row(key: str, value: str) -> ft.Row:
            return ft.Row([
                ft.Text(f"{key}:", size=13, width=130,
                        color=ft.Colors.GREY_700),
                ft.Text(str(value), size=13, expand=True, selectable=True),
            ], spacing=6)

        self.detail_info.controls.extend([
            row("Версия", f"{info['version_name']} ({info['version_code']})"),
            row("Min SDK", info["min_sdk"]),
            row("Target SDK", info["target_sdk"]),
            row("Установщик", info["installer"]),
            row("Перв. установка", info["first_install"]),
            row("Обновлён", info["last_update"]),
        ])

        if info["permissions"]:
            self.detail_info.controls.append(ft.Divider(height=8))
            self.detail_info.controls.append(
                ft.Text(f"Разрешения ({len(info['permissions'])})",
                        size=14, weight=ft.FontWeight.W_600)
            )
            for perm in info["permissions"][:30]:
                self.detail_info.controls.append(
                    ft.Text(f"• {perm}", size=12,
                            color=ft.Colors.GREY_800)
                )
            if len(info["permissions"]) > 30:
                self.detail_info.controls.append(
                    ft.Text(f"… ещё {len(info['permissions']) - 30}",
                            size=12, color=ft.Colors.GREY_600)
                )

        if info["activities"]:
            self.detail_info.controls.append(ft.Divider(height=8))
            self.detail_info.controls.append(
                ft.Text(f"Activities ({len(info['activities'])})",
                        size=14, weight=ft.FontWeight.W_600)
            )
            for act in info["activities"][:15]:
                self.detail_info.controls.append(
                    ft.Text(f"• {act}", size=12,
                            color=ft.Colors.GREY_800, selectable=True)
                )

        self.launch_btn.visible = True
        self.page.update()

    # ─── Запуск приложения ───

    async def _on_launch(self, e):
        if not self.selected:
            return
        self.launch_btn.disabled = True
        self.status.value = f"Запускаю {self.selected}…"
        self.page.update()

        loop = asyncio.get_event_loop()
        ok, msg = await loop.run_in_executor(
            None, lambda: launch_package(self.selected))

        self.status.value = ("✅ " if ok else "⚠️ ") + msg
        self.launch_btn.disabled = False
        self.page.update()


def main(page: ft.Page):
    PackageViewerApp(page)


if __name__ == "__main__":
    ft.run(main)
