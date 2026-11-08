"""romm-shelf's stdout callback for ansible-playbook: one JSON object per task result.

patches.py reads these lines; anything else on stdout (warnings, a YAML error) is kept as text.
Several patches can run in one ansible-playbook, each imported by a wrapper with `x_job` in its
import vars: a play's start is reported as {"job": ...}, and each result names its job.
"""
import json

from ansible.plugins.callback import CallbackBase

JOB_VAR = "x_job"  # patches.JOB_VAR


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "stdout"
    CALLBACK_NAME = "romm_shelf"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._job = None

    def v2_playbook_on_play_start(self, play):
        # Plays run one after another, so every result until the next play is this job's.
        self._job = play.vars.get(JOB_VAR)
        if self._job is not None:
            print(json.dumps({"job": self._job}), flush=True)

    def _emit(self, result, status, item=False):
        r = result._result
        if not item and "results" in r:  # a loop: its items were reported one by one
            return
        name = result._task.get_name()
        if item:
            label = r.get("_ansible_item_label", r.get("item"))
            label = label.get("key", label) if isinstance(label, dict) else label
            name = f"{name} ({label})"
        diff = r.get("diff") or []
        msg = r.get("msg")
        if status == "failed" and not msg:
            msg = r.get("stderr") or r.get("module_stderr") or "failed"
        print(json.dumps({"task": name, "host": result._host.get_name(), "status": status, "changed": bool(r.get("changed")),
                          "msg": str(msg) if msg else None, "diff": diff if isinstance(diff, list) else [diff],
                          "job": self._job},
                         default=str), flush=True)

    def v2_runner_on_ok(self, result):
        self._emit(result, "ok")

    def v2_runner_on_failed(self, result, ignore_errors=False):
        self._emit(result, "ok" if ignore_errors else "failed")

    def v2_runner_on_skipped(self, result):
        self._emit(result, "skipped")

    def v2_runner_on_unreachable(self, result):
        self._emit(result, "unreachable")

    def v2_runner_item_on_ok(self, result):
        self._emit(result, "ok", item=True)

    def v2_runner_item_on_failed(self, result):
        self._emit(result, "failed", item=True)

    def v2_runner_item_on_skipped(self, result):
        self._emit(result, "skipped", item=True)
