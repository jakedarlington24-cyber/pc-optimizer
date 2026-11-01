import threading
import tkinter as tk
import tkinter.messagebox
import webbrowser
from tkinter import scrolledtext

import anthropic

from pc_tools import (
    ACTIONS_NEEDING_RESTORE_POINT,
    ACTIONS_REQUIRING_CONFIRMATION,
    MAX_TOKENS,
    MODEL,
    SYSTEM_PROMPT,
    TOOLS,
    create_restore_point,
    describe_action,
    get_api_key,
    load_history,
    run_tool,
    save_api_key,
    undo_change,
)

QUICK_ACTIONS = [
    [
        ("Scan my PC", "Scan my PC. Tell me what you found and give me a list of fixes ranked by impact."),
        ("Free up space", "How can I free up disk space on my PC?"),
        ("Find big files", "Find the largest files in my user folder so I can decide what to delete."),
        ("Speed up startup", "Which startup programs could I safely disable to make my PC start faster?"),
        ("What's slowing me down?", "What's using the most CPU and memory on my PC right now?"),
    ],
    [
        ("Internet problems", "My internet seems slow or unreliable. Diagnose it and suggest fixes."),
        ("Gaming boost", "Check my gaming and performance settings and my graphics driver, and suggest improvements."),
        ("Security check", "Check my PC for unwanted software, suspicious startup items, and browser extensions, and check my antivirus."),
        ("Updates & drivers", "Check for Windows updates and whether my drivers are up to date."),
    ],
]

API_KEYS_URL = "https://console.anthropic.com/settings/keys"


class ApiKeyDialog:
    """Asks for an Anthropic API key, checks it works, and saves it for future runs."""

    def __init__(self, parent, on_saved, on_cancel=None, message=None):
        self.on_saved = on_saved
        self.on_cancel = on_cancel
        self.win = tk.Toplevel(parent)
        self.win.title("PC Assistant - API key")
        self.win.resizable(False, False)
        self.win.protocol("WM_DELETE_WINDOW", self.cancel)

        body = tk.Frame(self.win, padx=20, pady=16)
        body.pack(fill="both")
        tk.Label(body, text="Connect PC Assistant to Claude", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        intro = message or (
            "PC Assistant uses Claude to understand your PC and explain what it finds.\n"
            "It needs an Anthropic API key, which is saved only on this PC."
        )
        tk.Label(body, text=intro, justify="left", font=("Segoe UI", 10)).pack(anchor="w", pady=(6, 10))

        link = tk.Label(body, text="Get a key at console.anthropic.com (opens your browser)",
                        fg="#1a5fb4", cursor="hand2", font=("Segoe UI", 10, "underline"))
        link.pack(anchor="w")
        link.bind("<Button-1>", lambda e: webbrowser.open(API_KEYS_URL))

        tk.Label(body, text="Paste your API key:", font=("Segoe UI", 10)).pack(anchor="w", pady=(12, 2))
        self.entry = tk.Entry(body, width=56, show="•", font=("Segoe UI", 10))
        self.entry.pack(anchor="w", fill="x")
        self.entry.bind("<Return>", lambda e: self.save())
        self.entry.focus_set()

        self.show_var = tk.BooleanVar(value=False)
        tk.Checkbutton(body, text="Show key", variable=self.show_var,
                       command=lambda: self.entry.config(show="" if self.show_var.get() else "•")).pack(anchor="w")

        self.status = tk.Label(body, text="", font=("Segoe UI", 9), justify="left", wraplength=420)
        self.status.pack(anchor="w", pady=(6, 0))

        buttons = tk.Frame(body)
        buttons.pack(fill="x", pady=(12, 0))
        self.save_button = tk.Button(buttons, text="Save and connect", command=self.save, width=16)
        self.save_button.pack(side="right")
        tk.Button(buttons, text="Cancel", command=self.cancel, width=10).pack(side="right", padx=(0, 6))

        self.win.lift()
        self.win.attributes("-topmost", True)
        self.win.after(500, lambda: self.win.attributes("-topmost", False))
        self.win.grab_set()

    def save(self):
        key = self.entry.get().strip().strip('"').strip("'")
        if not key:
            self.status.config(text="Paste your key first.", fg="#c01c28")
            return
        self.save_button.config(state="disabled")
        self.status.config(text="Checking the key...", fg="#888888")
        threading.Thread(target=self.check_key, args=(key,), daemon=True).start()

    def check_key(self, key):
        # Listing models is free, so it's a cheap way to confirm the key works
        try:
            anthropic.Anthropic(api_key=key).models.list(limit=1)
            error = None
        except anthropic.AuthenticationError:
            error = "That key was rejected. Make sure you copied the whole key (it starts with sk-ant-)."
        except anthropic.APIConnectionError:
            error = "Couldn't reach Anthropic. Check your internet connection."
        except Exception as e:
            error = f"Couldn't check the key: {e}"
        self.win.after(0, self.finish, key, error)

    def finish(self, key, error):
        if error:
            self.status.config(text=error, fg="#c01c28")
            self.save_button.config(state="normal")
            return
        save_api_key(key)
        self.win.grab_release()
        self.win.destroy()
        self.on_saved(key)

    def cancel(self):
        self.win.grab_release()
        self.win.destroy()
        if self.on_cancel:
            self.on_cancel()


class HistoryWindow:
    """Shows every change PC Assistant made, with a button to undo the selected one."""

    def __init__(self, app):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.title("PC Assistant - Change history")
        self.win.geometry("640x380")
        self.win.transient(app.root)

        tk.Label(self.win, text="Changes PC Assistant has made (newest first)",
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=12, pady=(12, 4))
        frame = tk.Frame(self.win)
        frame.pack(fill="both", expand=True, padx=12)
        scrollbar = tk.Scrollbar(frame)
        scrollbar.pack(side="right", fill="y")
        self.listbox = tk.Listbox(frame, font=("Segoe UI", 10), activestyle="none", yscrollcommand=scrollbar.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self.listbox.yview)
        self.listbox.bind("<<ListboxSelect>>", lambda e: self.update_button())

        bottom = tk.Frame(self.win)
        bottom.pack(fill="x", padx=12, pady=10)
        self.hint = tk.Label(bottom, text="", fg="#888888", font=("Segoe UI", 9))
        self.hint.pack(side="left")
        tk.Button(bottom, text="Close", command=self.win.destroy, width=10).pack(side="right")
        self.undo_button = tk.Button(bottom, text="Undo selected", command=self.undo_selected, width=14)
        self.undo_button.pack(side="right", padx=(0, 6))
        self.refresh()

    def refresh(self):
        self.changes = list(reversed(load_history()))
        self.listbox.delete(0, tk.END)
        for c in self.changes:
            if c.get("undone"):
                status = "undone"
            elif c.get("undo"):
                status = "can undo"
            else:
                status = "permanent"
            self.listbox.insert(tk.END, f"{c['time']}   {c['description']}   [{status}]")
            if c.get("undone"):
                self.listbox.itemconfig(tk.END, fg="#888888")
        if not self.changes:
            self.listbox.insert(tk.END, "No changes yet. Anything PC Assistant changes will be listed here.")
        self.update_button()

    def selected(self):
        sel = self.listbox.curselection()
        if not sel or not self.changes:
            return None
        return self.changes[sel[0]]

    def update_button(self):
        c = self.selected()
        can_undo = bool(c and c.get("undo") and not c.get("undone"))
        self.undo_button.config(state="normal" if can_undo else "disabled")
        if c and not c.get("undo"):
            self.hint.config(text="This kind of change can't be undone automatically.")
        else:
            self.hint.config(text="")

    def undo_selected(self):
        c = self.selected()
        if not c:
            return
        if not tkinter.messagebox.askyesno("Undo change", f"Undo this change?\n\n{c['description']}", parent=self.win):
            return
        result = undo_change(c["id"])
        self.app.append_message("System", result)
        self.refresh()


class PCAssistantApp:
    def __init__(self, root, client):
        self.root = root
        self.client = client
        root.title("PC Assistant")
        root.geometry("820x680")
        root.minsize(620, 400)

        self.quick_buttons = []
        for row_index, row in enumerate(QUICK_ACTIONS):
            quick_frame = tk.Frame(root)
            quick_frame.pack(padx=10, pady=(10 if row_index == 0 else 4, 0), fill="x")
            for label, prompt in row:
                b = tk.Button(quick_frame, text=label, command=lambda p=prompt: self.send_message(p))
                b.pack(side="left", padx=(0, 5))
                self.quick_buttons.append(b)
            if row_index == 0:
                self.clear_button = tk.Button(quick_frame, text="New chat", command=self.clear_chat)
                self.clear_button.pack(side="right")
            else:
                tk.Button(quick_frame, text="API key", command=self.open_key_dialog).pack(side="right")
                tk.Button(quick_frame, text="History", command=lambda: HistoryWindow(self)).pack(side="right", padx=(0, 5))

        self.chat_box = scrolledtext.ScrolledText(root, wrap=tk.WORD, state="disabled", font=("Segoe UI", 10))
        self.chat_box.pack(padx=10, pady=10, fill="both", expand=True)
        self.chat_box.tag_configure("You", foreground="#1a5fb4", font=("Segoe UI", 10, "bold"))
        self.chat_box.tag_configure("Assistant", foreground="#26a269", font=("Segoe UI", 10, "bold"))
        self.chat_box.tag_configure("System", foreground="#888888", font=("Segoe UI", 9, "italic"))
        self.chat_box.tag_configure("Error", foreground="#c01c28", font=("Segoe UI", 10, "bold"))

        bottom_frame = tk.Frame(root)
        bottom_frame.pack(padx=10, pady=(0, 5), fill="x")

        self.entry = tk.Entry(bottom_frame, font=("Segoe UI", 11))
        self.entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.entry.bind("<Return>", lambda event: self.send_message())
        self.entry.focus_set()

        self.send_button = tk.Button(bottom_frame, text="Send", command=self.send_message)
        self.send_button.pack(side="right")

        self.status = tk.Label(root, text="", anchor="w", fg="#888888", font=("Segoe UI", 9))
        self.status.pack(padx=10, pady=(0, 8), fill="x")

        self.conversation = []
        self.busy = False
        self.append_message("Assistant", "Hi! Click \"Scan my PC\" for a full check-up, or ask me anything about your PC.")

    # ---- UI helpers (must run on the Tk main thread) ----

    def append_message(self, sender, text):
        self.chat_box.configure(state="normal")
        self.chat_box.insert(tk.END, f"{sender}: ", sender)
        self.chat_box.insert(tk.END, f"{text}\n\n")
        self.chat_box.configure(state="disabled")
        self.chat_box.see(tk.END)

    def set_busy(self, busy, status_text=""):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in [self.send_button, self.clear_button, *self.quick_buttons]:
            b.config(state=state)
        self.status.config(text=status_text)

    def ui(self, func, *args):
        """Schedule a UI update from the worker thread onto the Tk main thread."""
        self.root.after(0, func, *args)

    def clear_chat(self):
        self.conversation = []
        self.chat_box.configure(state="normal")
        self.chat_box.delete("1.0", tk.END)
        self.chat_box.configure(state="disabled")
        self.append_message("Assistant", "Started a new chat. What would you like to know?")

    def open_key_dialog(self, message=None):
        def saved(key):
            self.client = anthropic.Anthropic(api_key=key)
            self.append_message("System", "API key saved. You're connected.")
        ApiKeyDialog(self.root, on_saved=saved, message=message)

    # ---- Chat logic ----

    def send_message(self, text=None):
        if self.busy:
            return
        user_input = (text or self.entry.get()).strip()
        if not user_input:
            return
        self.entry.delete(0, tk.END)
        self.append_message("You", user_input)
        self.set_busy(True, "Thinking...")
        threading.Thread(target=self.process_message, args=(user_input,), daemon=True).start()

    def process_message(self, user_input):
        turn_start = len(self.conversation)
        self.conversation.append({"role": "user", "content": user_input})
        try:
            self.run_conversation()
        except anthropic.AuthenticationError:
            del self.conversation[turn_start:]
            self.ui(self.append_message, "Error", "Your Anthropic API key was rejected. Enter a new one, then try again.")
            self.ui(self.open_key_dialog, "Your saved API key was rejected (it may have been deleted or mistyped).\nPaste a working key to continue.")
        except anthropic.APIConnectionError:
            del self.conversation[turn_start:]
            self.ui(self.append_message, "Error", "Couldn't reach Claude. Check your internet connection and try again.")
        except anthropic.APIError as e:
            del self.conversation[turn_start:]
            self.ui(self.append_message, "Error", f"Claude API error: {e}")
        except Exception as e:
            del self.conversation[turn_start:]
            self.ui(self.append_message, "Error", f"Something went wrong: {e}")
        finally:
            self.ui(self.set_busy, False)

    def run_conversation(self):
        while True:
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=self.conversation,
            )
            self.conversation.append({"role": "assistant", "content": response.content})

            for block in response.content:
                if block.type == "text" and block.text.strip():
                    self.ui(self.append_message, "Assistant", block.text)

            if response.stop_reason != "tool_use":
                if response.stop_reason == "max_tokens":
                    self.ui(self.append_message, "System", "(The reply was cut off because it was too long.)")
                return

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": self.handle_tool_call(block),
                    })
            self.conversation.append({"role": "user", "content": tool_results})
            self.ui(self.status.config, {"text": "Thinking..."})

    def handle_tool_call(self, block):
        if block.name not in ACTIONS_REQUIRING_CONFIRMATION:
            self.ui(self.status.config, {"text": f"Checking: {block.name.replace('_', ' ')}..."})
            result = run_tool(block.name, block.input)
            if block.name == "open_page":
                self.ui(self.append_message, "System", result)
            return result

        if not self.ask_confirmation(f"{describe_action(block.name, block.input)}\n\nProceed?"):
            self.ui(self.append_message, "System", f"You declined {block.name}.")
            return "User declined this action."

        note = ""
        if block.name in ACTIONS_NEEDING_RESTORE_POINT:
            self.ui(self.status.config, {"text": "Creating a System Restore point..."})
            ok, message = create_restore_point()
            self.ui(self.append_message, "System", message)
            if not ok:
                note = " (Note: a restore point could not be created before this action.)"

        self.ui(self.status.config, {"text": f"Running {block.name}..."})
        result = run_tool(block.name, block.input)
        self.ui(self.append_message, "System", result)
        return result + note

    def ask_confirmation(self, message):
        """Show a yes/no popup on the main thread and wait for the answer."""
        result = {"value": False}
        done = threading.Event()

        def show_popup():
            result["value"] = tkinter.messagebox.askyesno("Confirm Action", message, parent=self.root)
            done.set()

        self.root.after(0, show_popup)
        done.wait()
        return result["value"]


def main():
    root = tk.Tk()
    root.withdraw()  # stay hidden until we have an API key

    def start(key):
        root.deiconify()
        root.lift()
        root.attributes("-topmost", True)
        root.after(500, lambda: root.attributes("-topmost", False))
        PCAssistantApp(root, anthropic.Anthropic(api_key=key))

    key = get_api_key()
    if key:
        start(key)
    else:
        ApiKeyDialog(root, on_saved=start, on_cancel=root.destroy)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        input("\nAn error occurred. Press Enter to close...")
