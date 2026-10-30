"""A tiny, dependency-free stand-in for tkinter.

Lets the GUI module be exercised in CI (no display, no Tk bindings) while still
catching the things that actually break on macOS:

* Tk widgets being touched from a worker thread (recorded per call),
* ``after()`` callbacks not being pumped,
* any use of ``tk::PlaceWindow``/``eval`` at startup,
* dialogs shown at the wrong time.
"""

from __future__ import annotations

import sys
import threading
import types


class State:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.widget_calls: list[tuple[str, str, str]] = []  # (widget, method, thread)
        self.scheduled: list[tuple[float, object]] = []
        self.dialogs: list[tuple[str, str, str]] = []  # (kind, title, message)
        self.copy_buffer: str = ""
        self.open_dialog_result: str = ""
        self.askyesno_result: bool = False
        self.evals: list[str] = []
        self.destroyed: bool = False

    # -- helpers ---------------------------------------------------------
    def touch(self, widget: str, method: str) -> None:
        self.widget_calls.append((widget, method, threading.current_thread().name))

    def worker_touches(self) -> list[tuple[str, str, str]]:
        main = threading.main_thread().name
        return [call for call in self.widget_calls if call[2] != main]

    def pumps_after(self, limit: int = 60) -> None:
        """Run scheduled ``after`` callbacks (the app re-schedules its pump)."""
        for _ in range(limit):
            if not self.scheduled:
                return
            batch, self.scheduled = self.scheduled, []
            for _delay, callback in batch:
                if callback is not None:
                    callback()


state = State()


class _Widget:
    _name = "Widget"

    def __init__(self, master=None, **options) -> None:
        self.master = master
        self.options = dict(options)
        self._exists = True
        self._children = []
        if master is not None and hasattr(master, "_children"):
            master._children.append(self)
        state.touch(self._name, "__init__")

    # -- geometry / lifecycle -------------------------------------------
    def pack(self, *args, **kwargs) -> "_Widget":
        state.touch(self._name, "pack")
        return self

    def grid(self, *args, **kwargs) -> "_Widget":
        return self

    def place(self, *args, **kwargs) -> "_Widget":
        return self

    def destroy(self) -> None:
        state.touch(self._name, "destroy")
        self._exists = False

    def winfo_exists(self) -> int:
        return int(self._exists)

    def winfo_children(self) -> list:
        return []

    def winfo_screenwidth(self) -> int:
        return 1920

    def winfo_screenheight(self) -> int:
        return 1080

    # -- configuration ---------------------------------------------------
    def configure(self, **options):
        state.touch(self._name, "configure")
        self.options.update(options)
        return None

    config = configure

    def cget(self, key):
        return self.options.get(key, "")

    def __getitem__(self, key):
        return self.options.get(key)

    def __setitem__(self, key, value):
        state.touch(self._name, "setitem")
        self.options[key] = value

    # -- events ----------------------------------------------------------
    def bind(self, *args, **kwargs) -> None:
        return None

    def after(self, delay, callback=None, *args):
        state.scheduled.append((delay, (lambda: callback(*args)) if callback else None))
        return f"after#{len(state.scheduled)}"

    def protocol(self, *args, **kwargs) -> None:
        return None

    def lift(self) -> None:
        state.touch(self._name, "lift")

    def focus_force(self) -> None:
        state.touch(self._name, "focus_force")

    def deiconify(self) -> None:
        state.touch(self._name, "deiconify")

    def attributes(self, *args, **kwargs) -> None:
        state.touch(self._name, "attributes")

    def update_idletasks(self) -> None:
        state.touch(self._name, "update_idletasks")

    def update(self) -> None:
        return None

    def title(self, *args) -> None:
        return None

    def geometry(self, *args) -> None:
        return None

    def minsize(self, *args) -> None:
        return None

    def resizable(self, *args) -> None:
        return None

    def createcommand(self, *args) -> None:
        return None

    def clipboard_clear(self) -> None:
        state.copy_buffer = ""

    def clipboard_append(self, text: str) -> None:
        state.copy_buffer += text

    def quit(self) -> None:
        state.destroyed = True

    def mainloop(self, *args, **kwargs) -> None:
        # The app's own scheduled callbacks are pumped by the tests instead.
        return None

    def eval(self, script: str):
        state.evals.append(script)
        raise AssertionError(f"Tk eval is unsafe and should not be used: {script!r}")


class Tk(_Widget):
    _name = "Tk"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(None)
        self.TkVersion = 9.0  # module-level attribute is patched below


class _Var:
    """Tk variable double. Records the calling thread, like real widgets."""

    def __init__(self, value=None, **kwargs) -> None:
        self._value = value

    def get(self):
        state.touch(type(self).__name__, "get")
        return self._value

    def set(self, value) -> None:
        state.touch(type(self).__name__, "set")
        self._value = value


class BooleanVar(_Var):
    pass


class StringVar(_Var):
    pass


class IntVar(_Var):
    pass


class Menu(_Widget):
    _name = "Menu"

    def __init__(self, master=None, **options):
        super().__init__(master, **options)
        self.entries = []

    def add_command(self, label="", command=None, **kwargs):
        self.entries.append(("command", label, command))
        return label

    def add_cascade(self, label="", menu=None, **kwargs):
        self.entries.append(("cascade", label, menu))
        return label

    def add_separator(self, **kwargs):
        self.entries.append(("separator", "", None))

    def add_checkbutton(self, label="", variable=None, command=None, **kwargs):
        self.entries.append(("checkbutton", label, command))


def _make_widget_class(name: str):
    return type(name, (_Widget,), {"_name": name})


def install() -> State:
    """Register stub modules in ``sys.modules`` and return the recorder."""
    state.reset()

    tk = types.ModuleType("tkinter")
    tk.Tk = Tk
    tk.Tcl = lambda *a, **k: None
    tk.Toplevel = _make_widget_class("Toplevel")
    tk.Frame = _make_widget_class("Frame")
    tk.Label = _make_widget_class("Label")
    tk.Button = _make_widget_class("Button")
    tk.Checkbutton = _make_widget_class("Checkbutton")
    tk.Menu = Menu
    tk.Text = _make_widget_class("Text")
    tk.BooleanVar = BooleanVar
    tk.StringVar = StringVar
    tk.IntVar = IntVar
    tk.TkVersion = 9.0
    tk.TclVersion = 9.0

    ttk = types.ModuleType("tkinter.ttk")
    for name in ("Frame", "Label", "Button", "Progressbar", "Checkbutton", "Separator", "Style"):
        setattr(ttk, name, _make_widget_class(f"ttk.{name}"))
    tk.ttk = ttk

    filedialog = types.ModuleType("tkinter.filedialog")
    filedialog.askopenfilename = lambda **kwargs: state.open_dialog_result
    filedialog.askdirectory = lambda **kwargs: ""

    messagebox = types.ModuleType("tkinter.messagebox")
    messagebox.showinfo = lambda title, message, **kw: state.dialogs.append(("info", title, message))
    messagebox.showerror = lambda title, message, **kw: state.dialogs.append(("error", title, message))

    def askyesno(title, message, **kwargs):
        state.dialogs.append(("askyesno", title, message))
        return state.askyesno_result

    messagebox.askyesno = askyesno
    messagebox.askokcancel = askyesno

    scrolledtext = types.ModuleType("tkinter.scrolledtext")

    class ScrolledText(_Widget):
        _name = "ScrolledText"

        def __init__(self, master=None, **kwargs):
            super().__init__(master, **kwargs)
            self.content = ""

        def insert(self, index, text):
            self.content = text

    scrolledtext.ScrolledText = ScrolledText

    tk.filedialog = filedialog
    tk.messagebox = messagebox
    tk.scrolledtext = scrolledtext

    for name, module in (
        ("tkinter", tk),
        ("tkinter.ttk", ttk),
        ("tkinter.filedialog", filedialog),
        ("tkinter.messagebox", messagebox),
        ("tkinter.scrolledtext", scrolledtext),
    ):
        sys.modules[name] = module
    return state


def uninstall() -> None:
    for name in (
        "tkinter",
        "tkinter.ttk",
        "tkinter.filedialog",
        "tkinter.messagebox",
        "tkinter.scrolledtext",
    ):
        sys.modules.pop(name, None)
