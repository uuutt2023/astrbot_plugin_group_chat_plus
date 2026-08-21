"""
AstrBotConfig 兼容代理
=====================

`utils/config_adapter.py` 已经实现了扁平 <-> 模块化 的核心映射逻辑，
但插件代码中仍然充斥 ``self.config.get("xxx", default)`` 的调用，
全部改成 ``adapter.get(...)`` 既不安全也不易回滚。

本模块提供一个轻量代理类 ``ModularConfigProxy``，它对外暴露与
AstrBot 原生 ``AstrBotConfig`` 完全一致的 ``get`` / ``__contains__``
/ ``__getitem__`` 接口，但内部行为是：

1. **读取路径**：永远优先调用原始 ``AstrBotConfig.get(key, default)``，
   这样能保证拿到 AstrBot 实际生效的用户配置值（包括从 schema 解析
   出来的默认值）。这一步对调用方完全透明，行为与改造前完全一致。

2. **写入路径**：
   - ``proxy.set(key, value)`` 会同时写入内存中的模块化结构
     （通过适配器映射到正确的模块化路径），方便后续调用
     ``adapter.to_modular_dict()`` / ``adapter.save()`` 持久化；
   - 如果传入的 ``key`` 是未经注册的扁平 key，代理还会把它透传
     到 ``raw.set`` （如果存在），保持与原 AstrBotConfig 的兼容性。

3. **特殊便利方法**：
   - ``proxy.apply_modular_overrides(mapping)`` 把 ``{flat_key: value}``
     形式的字典批量应用到模块化结构（用于从 Web 面板保存配置）；
   - ``proxy.get_schema(key)`` 返回模块化 schema 中的整条配置项
     字典（含 description / hint / type / default 等）。

这样一来，原有 ``self.config.get("xxx", default)`` 这种调用方式
**不需要修改任何一行**，同时把扁平配置 -> 模块化结构的双向同步
能力以"写入时生效"的形式暴露出来。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .config_adapter import FlatToModularAdapter


class ModularConfigProxy:
    """AstrBotConfig 的薄封装，叠加扁平->模块化路径自动转换能力。

    使用示例
    --------
    >>> proxy = ModularConfigProxy(config, adapter)
    >>> proxy.get("enable_group_chat", True)        # 走 raw.get，保持原行为
    >>> proxy.get_schema("enable_group_chat")        # 返回 schema 项
    >>> proxy.set("enable_group_chat", False)        # 写入模块化结构
    >>> proxy.apply_modular_overrides({"enable_group_chat": False})
    """

    __slots__ = ("_raw", "_adapter",)

    def __init__(self, raw_config: Any, adapter: FlatToModularAdapter) -> None:
        self._raw = raw_config
        self._adapter = adapter

    # ------------------------------------------------------------------
    # 读取接口：始终走 raw_config，保证拿到用户实际配置值
    # ------------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        return self._safe_raw_get(key, default)

    def get_schema(self, key: str) -> Optional[Dict[str, Any]]:
        """返回模块化 schema 中该 key 对应的整条配置项字典。

        - ``key`` 可以是扁平 key（已注册）或模块化路径；
        - 如果两者都找不到，返回 None。
        """
        try:
            path = self._adapter._resolve_path(key)  # noqa: SLF001
            return self._adapter._get_modular_item(path)  # noqa: SLF001
        except Exception:
            return None

    def _safe_raw_get(self, key: str, default: Any) -> Any:
        """回退到原始 AstrBotConfig.get，异常时返回 default。"""
        raw = self._raw
        if raw is None:
            return default
        try:
            getter = getattr(raw, "get", None)
            if callable(getter):
                return getter(key, default)
        except Exception:
            pass
        # 兜底：如果 raw 是 dict
        try:
            if isinstance(raw, dict):
                return raw.get(key, default)
        except Exception:
            pass
        return default

    # ------------------------------------------------------------------
    # 字典风格接口
    # ------------------------------------------------------------------
    def __getitem__(self, key: str) -> Any:
        result = self.get(key)
        if result is None:
            raise KeyError(f"Key '{key}' not found.")
        return result

    def __contains__(self, key: str) -> bool:
        # 1) 优先判定 raw_config 中是否存在该 key
        if self._key_in_raw(key):
            return True
        # 2) 然后判定模块化 schema 中是否包含
        try:
            path = self._adapter._resolve_path(key)  # noqa: SLF001
            if self._adapter._get_modular_item(path) is not None:  # noqa: SLF001
                return True
        except Exception:
            pass
        return False

    def _key_in_raw(self, key: str) -> bool:
        """尝试用 raw_config 的 has 风格判定。"""
        raw = self._raw
        if raw is None:
            return False
        # 1) dict 类型直接 __contains__
        try:
            if isinstance(raw, dict):
                return key in raw
        except Exception:
            pass
        # 2) 自定义对象：尝试 ``in`` 操作符；若不支持则吞掉异常
        try:
            if hasattr(raw, "__contains__"):
                return bool(key in raw)
        except Exception:
            pass
        return False

    # ------------------------------------------------------------------
    # 写入接口：同时更新 raw_config 与模块化结构
    # ------------------------------------------------------------------
    def set(self, key: str, value: Any, *, sync_raw: bool = True) -> List[str]:
        """写入配置。

        - 始终通过 :meth:`FlatToModularAdapter.set` 更新模块化结构；
        - 当 ``sync_raw=True`` 且原 AstrBotConfig 暴露 ``__setitem__``
          或 ``save`` 接口时，尝试同步写回原对象；
        - 返回被修改的模块化路径列表。
        """
        paths = self._adapter.set(key, value)
        if sync_raw:
            self._sync_raw_set(key, value)
        return paths

    def update(self, mapping: Dict[str, Any], *, sync_raw: bool = True) -> List[str]:
        """批量写入。"""
        paths = self._adapter.update(mapping)
        if sync_raw:
            for key, value in mapping.items():
                self._sync_raw_set(key, value)
        return paths

    def apply_modular_overrides(self, mapping: Dict[str, Any]) -> List[str]:
        """便捷别名：仅更新模块化结构，不回写 raw_config。

        适用于从可视化 Web 面板保存配置的场景：此时调用方通常已经把
        数据持久化到了 AstrBot 的存储后端，不需要再通过 raw.set
        二次写入。
        """
        return self._adapter.update(mapping)

    def _sync_raw_set(self, key: str, value: Any) -> None:
        """尽量把 ``set`` 同步回原 AstrBotConfig / dict（可选）。"""
        raw = self._raw
        if raw is None:
            return
        try:
            # 优先尝试 __setitem__
            raw[key] = value
            return
        except Exception:
            pass
        try:
            setter = getattr(raw, "set", None)
            if callable(setter):
                setter(key, value)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 暴露底层对象，便于高级用法
    # ------------------------------------------------------------------
    @property
    def raw(self) -> Any:
        """原始 AstrBotConfig 对象。"""
        return self._raw

    @property
    def adapter(self) -> FlatToModularAdapter:
        """底层适配器。"""
        return self._adapter

    # ------------------------------------------------------------------
    # 调试 / 持久化便捷方法
    # ------------------------------------------------------------------
    def save_modular(self, path: str) -> None:
        """把当前模块化结构（含本会话 set 的修改）落盘到指定 JSON 文件。"""
        self._adapter.save(path)

    def debug_summary(self) -> Dict[str, Any]:
        return self._adapter.debug_summary()