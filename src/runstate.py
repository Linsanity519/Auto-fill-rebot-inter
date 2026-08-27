"""断点：哪些已经跑成功了，重跑时跳过。

## 为什么单独抽出来

原来 `runner` / `dmp_runner` / `ab_runner` 各写了一份一模一样的
`_load_state` / `_save_state` / `clear_state`，而 **`wizard_runner` /
`pp_runner` / `ad_runner` 一份都没有** —— 这三个恰好是最该有的：
一个单元要跑几分钟，而且**会在后台真的建出活动和单元**。
40 个单元跑到第 30 个失败，重跑会把前 29 个再建一遍。

界面上那两处也因此是坏的：
  · 「跳过已成功的」—— 这三个的 preview() 里 done 写死 False，永远不跳
  · 「清除断点」—— webapp 无条件调 runner.clear_state()，这三个没这个方法，
    直接 AttributeError，界面上显示「清除断点失败」

所以这不是"代码重复"的问题，是"漏了的地方没人发现"的问题 —— 正是该归一化的那类。

## 存盘格式（没变，老的 output/state.json 直接能用）

    {
      "资源位投放": {"done": [key, ...], "failed": [{"key":…, "name":…, "error":…}]},
      "价格配置":   {"done": [0, 1, 2], "failed": []}
    }

⚠ 按**配置类型名**分区，各跑各的互不干扰。
⚠ key 的类型由调用方定，只要能进 JSON 又能相等比较就行：
  价格配置用行号（int），DMP/AB 用人群ID/实验ID（str），
  资源位投放用「资源位/单元名」（str）。

## key 怎么选

选**重跑时还认得出是同一条**的东西：

  好： 人群ID、实验ID、「资源位/单元名」、Excel 行号 + 单元名
  坏： 列表里的下标（翻页顺序会变）、时间戳、页面上的行号

⚠ 拿 Excel 行号当 key 的，用户在 Excel 中间插一行，断点就对不上了 ——
  所以能带上名字就带上（`f"{row}/{name}"`），名字变了本来也该当成新的一条。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class RunState:
    """一个配置类型的断点。构造时读盘，每次标记后立刻写盘。

    ⚠ 每标记一条就写一次盘，不攒着 —— 跑到一半被关掉、断网、程序崩，
      已经成功的那些必须还在。一次写盘几毫秒，而重跑一个单元要几分钟。
    """

    def __init__(self, path: str | Path, form_name: str, resume: bool = True):
        self.path = Path(path)
        self.form_name = form_name
        self.done: list = []
        self.failed: list = []
        if resume:
            self._load()

    # ---------------- 读写 ----------------
    def _load(self):
        if not self.path.exists():
            return
        try:
            all_state = json.loads(self.path.read_text(encoding="utf-8"))
            mine = all_state.get(self.form_name) or {}
            self.done = list(mine.get("done") or [])
            self.failed = list(mine.get("failed") or [])
        except Exception:
            # ⚠ 断点读不了绝不能挡住跑。最坏结果是从头跑一遍，
            #   而抛出去的话用户连界面都进不来。
            log.warning("断点文件读不了，当作从头开始：%s", self.path, exc_info=True)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        all_state = {}
        if self.path.exists():
            try:
                all_state = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                # 整个文件坏了就重写一份。别的配置类型的断点会丢，
                # 但它本来也已经读不出来了。
                log.warning("断点文件坏了，重写一份：%s", self.path, exc_info=True)
                all_state = {}
        if not isinstance(all_state, dict):
            all_state = {}
        all_state[self.form_name] = {"done": self.done, "failed": self.failed}
        try:
            self.path.write_text(
                json.dumps(all_state, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            # 写不了也不该挡住跑（比如目录被占用），下次重跑就是多跑几条
            log.warning("断点写不进去：%s", self.path, exc_info=True)

    # ---------------- 用的 ----------------
    def is_done(self, key) -> bool:
        return key in self.done

    def mark_done(self, key):
        if key not in self.done:
            self.done.append(key)
        self.save()

    def mark_failed(self, key, name: str, error: str):
        self.failed.append({"key": key, "name": name, "error": error})
        self.save()

    def clear(self):
        self.done, self.failed = [], []
        self.save()

    def __len__(self) -> int:
        return len(self.done)


class StateMixin:
    """给 Runner 用的三个方法。混进去就有断点，`self.s` / `self.f` 是现成的。

    用法（构造函数里）：

        self._init_state()          # 之后 self.state 就是 RunState

    然后：

        if self.state.is_done(key): 跳过
        ...成功后... self.state.mark_done(key)
        ...失败后... self.state.mark_failed(key, name, msg)

    ⚠ `clear_state()` 由这里统一提供 —— webapp 的「清除断点」是无条件调它的，
      少一个 mode 没有就是一个 AttributeError。
    """

    def _init_state(self):
        self.state = RunState(self.s["state_file"], self.f["name"],
                              resume=bool(self.s.get("resume")))

    def clear_state(self):
        self.state.clear()
