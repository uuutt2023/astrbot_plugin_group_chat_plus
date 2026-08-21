"""
扁平配置 <-> 模块化配置 兼容层
===============================

背景说明
--------
本插件的 AstrBot 配置文件 `_conf_schema.json` 原本是扁平化设计的
（例如：`enable_group_chat`、`decision_ai_provider_id`、`web_panel_port`）。

为了给可视化 Web 配置面板提供更友好的"分类卡片"体验，我们把 schema
切换成了模块化结构：每个顶层 key 是一个模块（如 `group_base`、
`decision_ai`、`web_panel` 等），实际配置项都放在该模块的 `items`
子字典里（例如 `group_base.items.enable_group_chat`）。

但是插件代码中有大量 `config.get("enable_group_chat")` 这种调用，
重写所有调用点风险极高。本模块提供一个**扁平 <-> 模块化**的双向
兼容层，让插件代码继续使用扁平 key 读取，同时：

1. 自动建立「扁平键 → 模块化路径」的映射表（基于 type / default /
   description 三元签名匹配）；
2. 提供 `get` / `__getitem__` 等读取接口，行为与 `config.get()`
   完全一致（接受默认值参数）；
3. 提供 `set` / `__setitem__` / `update` 等修改接口，对扁平 key
   的写入会自动反映到内存中的模块化结构里；
4. 提供 `register_legacy_mapping` / `save` 等高级接口，方便迁移
   与持久化。

使用方式
--------
- 直接替换插件里 `config.get("xxx", default)` 调用为：
      from .utils.config_adapter import get_config_adapter
      cfg = get_config_adapter()           # 模块级单例
      value = cfg.get("enable_group_chat", True)
- 或通过快捷函数：
      from .utils.config_adapter import cfg_get, cfg_set
      value = cfg_get("enable_group_chat", True)
      cfg_set("enable_group_chat", False)
- 兼容层会在初始化时按需懒加载单例；如需重置，调用
  `reset_config_adapter()`。
"""

from __future__ import annotations

import copy
import json
import os
import threading
from typing import Any, Dict, List, Optional, Tuple


# ----------------------------------------------------------------------
# 类型别名
# ----------------------------------------------------------------------
FlatKey = str                    # 旧版扁平 key，如 "enable_group_chat"
ModularPath = str                # 模块化路径，如 "group_base.items.enable_group_chat"
LegacyConfigDict = Dict[FlatKey, Dict[str, Any]]
ModularConfigDict = Dict[str, Any]


# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------
def _signature(item: Dict[str, Any]) -> Tuple[Any, Any, str]:
    """提取一个配置项的稳定三元签名，用于跨 schema 的字段匹配。

    使用 ``type`` / ``default`` / ``description`` 三元组，
    这样即使扁平 key 的命名与模块化路径不一致，只要三要素相同
    即可视为同一项。
    """
    return (
        item.get("type"),
        item.get("default"),
        item.get("description", ""),
    )


def _flatten_modular(config: ModularConfigDict) -> Dict[ModularPath, Dict[str, Any]]:
    """把模块化配置彻底展开为 "模块化路径 -> 配置项字典" 的映射。"""

    result: Dict[ModularPath, Dict[str, Any]] = {}

    def _walk(obj: Any, parts: List[str]) -> None:
        if not isinstance(obj, dict):
            return
        # 标准模块块：包含 "items" 子字典
        if "items" in obj and isinstance(obj["items"], dict):
            for key, value in obj["items"].items():
                full_path = ".".join(parts + ["items", key])
                result[full_path] = value
            # 注意：不递归 items 之外的内容，保持路径稳定
            return
        # 普通嵌套字典：递归
        for key, value in obj.items():
            _walk(value, parts + [key])

    _walk(config, [])
    return result


# ----------------------------------------------------------------------
# 主适配器
# ----------------------------------------------------------------------
class FlatToModularAdapter:
    """扁平 <-> 模块化 配置的双向兼容层。

    核心能力
    --------
    - 自动映射：通过 ``register_legacy_mapping`` 把旧版扁平配置（典型
      来源是 ``_conf_schema.json`` 的历史版本）逐项匹配到当前模块化
      schema。
    - 透明读取：``get`` / ``__getitem__`` 接受旧的扁平 key，自动落到
      模块化路径；找不到时返回调用方传入的默认值。
    - 双向写入：``set`` / ``__setitem__`` 既支持扁平 key 也支持完整
      模块化路径，写入会同时更新内存中的模块化配置。
    - 增量补丁：``update`` 接受一组扁平 key -> value 的字典，批量
      应用修改并返回被改动的模块化路径集合。
    - 持久化：``to_modular_dict`` / ``to_flat_dict`` / ``save`` 把
      当前内存状态导出或落盘到 JSON 文件。

    注意事项
    --------
    - 同一实例内部会维护一个 `_flat_map`，该映射应当来自 _conf_schema
      的"扁平版快照"。如果调用方只传入模块化配置而没有扁平版快照，
      ``get`` / ``set`` 等会回退到"按字面 key 当作模块化路径"处理。
    - 适配器对原始 ``modular_config`` 字典做**浅共享**：``get`` 返回
      的值是引用；``set`` 会直接修改 ``modular_config`` 中的相应位置。
      如需隔离，请自行 ``copy.deepcopy`` 后再传入。
    """

    # ------------------------------------------------------------------
    # 构造与映射
    # ------------------------------------------------------------------
    def __init__(self, modular_config: ModularConfigDict) -> None:
        self._config = modular_config
        # 扁平 key -> 模块化路径
        self._flat_map: Dict[FlatKey, ModularPath] = {}
        # 额外维护一个反向映射，方便按模块化路径反查所有引用扁平 key
        self._reverse_map: Dict[ModularPath, List[FlatKey]] = {}
        # 未匹配的扁平 key 缓存（避免每次都打印一次警告）
        self._unmatched_cache: set = set()
        # 线程安全：适配器可能在异步 handler 中被访问
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 映射注册
    # ------------------------------------------------------------------
    def register_legacy_mapping(self, flat_config: LegacyConfigDict) -> None:
        """根据旧版扁平 schema 构建 ``flat_key -> 模块化路径`` 映射。

        :param flat_config: 旧版扁平配置字典，例如原 ``_conf_schema.json``
            加载后的内容（每个 value 都包含 ``type`` / ``default`` /
            ``description`` 等字段）。
        """
        with self._lock:
            flat_paths = _flatten_modular(self._config)
            path_sigs = {path: _signature(item) for path, item in flat_paths.items()}

            self._flat_map.clear()
            self._reverse_map.clear()

            for flat_key, flat_item in flat_config.items():
                # 仅匹配形如配置项的字典（title 类的字符串会被忽略）
                if not isinstance(flat_item, dict):
                    continue

                sig = _signature(flat_item)
                matched_path: Optional[ModularPath] = None
                for path, path_sig in path_sigs.items():
                    if sig == path_sig:
                        matched_path = path
                        break

                if matched_path is None:
                    if flat_key not in self._unmatched_cache:
                        self._unmatched_cache.add(flat_key)
                        print(
                            f"[ConfigAdapter][Warning] 无法精确匹配旧键: "
                            f"{flat_key} (type={sig[0]}, default={sig[1]!r})"
                        )
                    continue

                self._flat_map[flat_key] = matched_path
                self._reverse_map.setdefault(matched_path, []).append(flat_key)

    # ------------------------------------------------------------------
    # 路径解析
    # ------------------------------------------------------------------
    def _resolve_path(self, key: str) -> ModularPath:
        """把任意 key 解析为模块化路径。"""
        if key in self._flat_map:
            return self._flat_map[key]
        # 已包含 ".items." 或以 ".items" 结尾，直接视为模块化路径
        if ".items" in key:
            return key
        # 兜底：把 key 当成模块化路径返回
        return key

    def _get_modular_item(self, path: ModularPath) -> Optional[Dict[str, Any]]:
        """按模块化路径取值；找不到返回 None。"""
        node: Any = self._config
        for part in path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return None
        return node if isinstance(node, dict) else None

    def _set_modular_item(self, path: ModularPath, value: Any) -> bool:
        """按模块化路径写入。返回是否成功。"""
        parts = path.split(".")
        if not parts:
            return False
        node: Any = self._config
        for part in parts[:-1]:
            if part not in node or not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = value
        return True

    # ------------------------------------------------------------------
    # 读取接口
    # ------------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        """兼容 ``config.get(key, default)`` 的读取。

        - ``key`` 可以是旧版扁平 key（如 ``enable_group_chat``），
          也可以是模块化路径（如 ``group_base.items.enable_group_chat``）。
        - 若该模块化项存在，返回其 ``default`` 字段值（即 schema 里
          标注的默认值，对应于运行态的实际生效值）。
        - 若不存在则回退到调用方传入的 ``default``。
        """
        with self._lock:
            path = self._resolve_path(key)
            item = self._get_modular_item(path)
            if item is None:
                return default
            # 兼容语义：item 里若有 default 字段，则用它；否则返回整个 dict
            if isinstance(item, dict) and "default" in item:
                return item.get("default", default)
            return item if item is not None else default

    def get_schema(self, key: str) -> Optional[Dict[str, Any]]:
        """返回模块化项的完整字典（含 description / hint / type 等）。

        与 ``get`` 的区别：本接口始终返回 ``items`` 下的整条配置项，
        而不是 ``default`` 字段。"""
        with self._lock:
            path = self._resolve_path(key)
            return self._get_modular_item(path)

    def __getitem__(self, key: str) -> Any:
        result = self.get(key)
        if result is None:
            raise KeyError(f"Key '{key}' not found in mapping or config.")
        return result

    # ------------------------------------------------------------------
    # 写入接口
    # ------------------------------------------------------------------
    def set(self, key: str, value: Any, *, persist: bool = False,
            config_path: Optional[str] = None) -> List[ModularPath]:
        """写入一个配置项。

        :param key: 扁平 key 或模块化路径。
        :param value: 期望写入的值。
        :param persist: 是否立即落盘。
        :param config_path: 落盘文件路径（仅在 ``persist=True`` 时生效）。
        :return: 被修改的模块化路径列表（一般为 1 条，便于上层做批量校验）。
        """
        with self._lock:
            path = self._resolve_path(key)
            ok = self._set_modular_item(path, value)
            if not ok:
                return []
            if persist and config_path:
                self.save(config_path)
            return [path]

    def update(self, mapping: Dict[str, Any], *, persist: bool = False,
               config_path: Optional[str] = None) -> List[ModularPath]:
        """批量写入。

        :param mapping: 扁平 key -> value 的字典。
        :param persist: 是否在全部写入后落盘。
        :param config_path: 落盘路径。
        :return: 被修改的模块化路径列表。
        """
        changed: List[ModularPath] = []
        with self._lock:
            for key, value in mapping.items():
                paths = self.set(key, value)
                changed.extend(paths)
            if persist and config_path:
                self.save(config_path)
        return changed

    def __setitem__(self, key: str, value: Any) -> None:
        self.set(key, value)

    # ------------------------------------------------------------------
    # 调试 / 持久化
    # ------------------------------------------------------------------
    def to_modular_dict(self) -> ModularConfigDict:
        """返回模块化配置的深拷贝，供外部读取。"""
        with self._lock:
            return copy.deepcopy(self._config)

    def to_flat_dict(self) -> Dict[FlatKey, Any]:
        """把所有扁平 key 映射到当前模块化项的 ``default`` 值。"""
        with self._lock:
            flat: Dict[FlatKey, Any] = {}
            for flat_key, path in self._flat_map.items():
                item = self._get_modular_item(path)
                if isinstance(item, dict) and "default" in item:
                    flat[flat_key] = item.get("default")
                elif item is not None:
                    flat[flat_key] = item
            return flat

    def save(self, path: str) -> None:
        """把当前模块化配置以 pretty JSON 形式落盘。"""
        with self._lock:
            directory = os.path.dirname(os.path.abspath(path))
            if directory and not os.path.exists(directory):
                os.makedirs(directory, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._config, f, ensure_ascii=False, indent=2)

    def debug_summary(self) -> Dict[str, Any]:
        """输出当前映射的统计信息，方便排错。"""
        with self._lock:
            return {
                "flat_key_count": len(self._flat_map),
                "modular_path_count": len(self._reverse_map),
                "unmatched_flat_keys": sorted(self._unmatched_cache),
                "sample_mapping": dict(list(self._flat_map.items())[:10]),
            }


# ----------------------------------------------------------------------
# 模块级单例 + 便捷函数
# ----------------------------------------------------------------------
_DEFAULT_ADAPTER: Optional[FlatToModularAdapter] = None
_DEFAULT_ADAPTER_LOCK = threading.Lock()
_DEFAULT_LEGACY_SNAPSHOT: Optional[LegacyConfigDict] = None


def _load_modular_from_disk(schema_path: str) -> ModularConfigDict:
    """从磁盘读取模块化 schema（_conf_schema.json）作为基础结构。"""
    with open(schema_path, "r", encoding="utf-8") as f:
        return json.load(f)


def init_adapter(modular_config: ModularConfigDict,
                 legacy_flat_snapshot: Optional[LegacyConfigDict] = None,
                 *, force: bool = False) -> FlatToModularAdapter:
    """初始化（或重置）模块级单例适配器。

    :param modular_config: 模块化配置字典（通常来自当前 _conf_schema.json）。
    :param legacy_flat_snapshot: 可选的旧版扁平 schema 快照；
        若提供则自动构建映射表。
    :param force: True 时强制覆盖已有单例（用于热重载等场景）。
    """
    global _DEFAULT_ADAPTER, _DEFAULT_LEGACY_SNAPSHOT
    with _DEFAULT_ADAPTER_LOCK:
        if _DEFAULT_ADAPTER is not None and not force:
            return _DEFAULT_ADAPTER
        adapter = FlatToModularAdapter(modular_config)
        if legacy_flat_snapshot:
            adapter.register_legacy_mapping(legacy_flat_snapshot)
        _DEFAULT_ADAPTER = adapter
        _DEFAULT_LEGACY_SNAPSHOT = legacy_flat_snapshot
        return adapter


def init_adapter_from_schema_path(schema_path: str,
                                  legacy_flat_snapshot: Optional[LegacyConfigDict] = None,
                                  *, force: bool = False) -> FlatToModularAdapter:
    """从磁盘上的 _conf_schema.json 初始化单例适配器。"""
    modular = _load_modular_from_disk(schema_path)
    return init_adapter(modular, legacy_flat_snapshot, force=force)


def get_config_adapter() -> FlatToModularAdapter:
    """获取模块级单例；如尚未初始化则抛出明确错误。"""
    global _DEFAULT_ADAPTER
    with _DEFAULT_ADAPTER_LOCK:
        if _DEFAULT_ADAPTER is None:
            raise RuntimeError(
                "ConfigAdapter 尚未初始化，请先在插件入口调用 "
                "init_adapter_from_schema_path() 或 init_adapter()。"
            )
        return _DEFAULT_ADAPTER


def reset_config_adapter() -> None:
    """重置模块级单例（主要用于测试）。"""
    global _DEFAULT_ADAPTER, _DEFAULT_LEGACY_SNAPSHOT
    with _DEFAULT_ADAPTER_LOCK:
        _DEFAULT_ADAPTER = None
        _DEFAULT_LEGACY_SNAPSHOT = None


# ---- 顶层便捷函数（与 dict 风格保持一致） ----
def cfg_get(key: str, default: Any = None) -> Any:
    """``get_config_adapter().get(key, default)`` 的顶层封装。"""
    return get_config_adapter().get(key, default)


def cfg_set(key: str, value: Any, *, persist: bool = False,
            config_path: Optional[str] = None) -> List[ModularPath]:
    """``get_config_adapter().set(key, value, ...)`` 的顶层封装。"""
    return get_config_adapter().set(key, value, persist=persist, config_path=config_path)


def cfg_update(mapping: Dict[str, Any], *, persist: bool = False,
               config_path: Optional[str] = None) -> List[ModularPath]:
    """``get_config_adapter().update(mapping, ...)`` 的顶层封装。"""
    return get_config_adapter().update(mapping, persist=persist, config_path=config_path)


# ----------------------------------------------------------------------
# 自检 / 自描述样例
# ----------------------------------------------------------------------
if __name__ == "__main__":
    # 这里给一段极简自检，演示整套用法；
    # 真实项目中，legacy_flat_snapshot 会从仓库里那份"旧版扁平 schema"
    # （例如本次提交里被替换掉的 _conf_schema.json）加载得到。
    sample_modular = {
        "group_base": {
            "description": "group_base",
            "type": "object",
            "items": {
                "enable_group_chat": {
                    "description": "启用群聊功能",
                    "type": "bool",
                    "hint": "总开关",
                    "default": True,
                },
                "initial_probability": {
                    "description": "初始概率",
                    "type": "float",
                    "default": 0.3,
                },
            },
        },
    }
    sample_legacy = {
        "enable_group_chat": {
            "description": "启用群聊功能",
            "type": "bool",
            "default": True,
        },
        "initial_probability": {
            "description": "初始概率",
            "type": "float",
            "default": 0.3,
        },
    }

    adapter = FlatToModularAdapter(sample_modular)
    adapter.register_legacy_mapping(sample_legacy)

    # 读取
    assert adapter.get("enable_group_chat") is True
    assert adapter.get("group_base.items.initial_probability") == 0.3

    # 修改 + 回读
    adapter.set("enable_group_chat", False)
    assert adapter.get("enable_group_chat") is False
    assert adapter.get("group_base.items.enable_group_chat") is False

    print("ConfigAdapter 自检通过：")
    print(json.dumps(adapter.debug_summary(), ensure_ascii=False, indent=2))