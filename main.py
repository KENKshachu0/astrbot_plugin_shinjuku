from __future__ import annotations

import re
import shlex
import sqlite3
from datetime import datetime
from os import path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .shinjuku_service import ShinjukuError, ShinjukuService


def _money(value: Any) -> str:
    number = float(value or 0)
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _dt(value: Any) -> str:
    if not value:
        return "永不过期"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    return value.strftime("%Y/%m/%d %H:%M:%S")


def _duration(minutes: int) -> str:
    if minutes >= 60:
        return f"{minutes // 60}小时{minutes % 60}分钟"
    return f"{minutes}分钟"


def _format_time_range(start: datetime, end: datetime) -> str:
    if start.date() == end.date():
        return f"{start:%H:%M:%S} - {end:%H:%M:%S}"
    return f"{start:%m/%d %H:%M:%S} - {end:%m/%d %H:%M:%S}"


def _format_wallet(wallet: dict[str, Any], currency: str) -> str:
    lines = [
        "--- 钱包 ---",
        f"总余额: {_money(wallet['total']['available'])}/{_money(wallet['total']['all'])} {currency}",
        f"付费余额: {_money(wallet['paid']['available'])} {currency}",
        f"免费余额: {_money(wallet['free']['available'])}/{_money(wallet['free']['all'])} {currency}",
        f"积分: {_money(wallet['points']['available'])}",
        f"优惠券: {wallet['tickets']['available']}/{wallet['tickets']['all']} 张",
        f"通行证: {wallet['passes']['available']}/{wallet['passes']['all']} 个",
    ]
    return "\n".join(lines)


def _format_pricing(cfg: dict[str, Any], currency: str) -> str:
    day_price = int(cfg.get("day_price") or 12)
    day_price_pass = int(cfg.get("day_price_pass") or 11)
    day_cap = int(cfg.get("day_cap") or 69)
    day_cap_pass = int(cfg.get("day_cap_pass") or 59)
    night_price = int(cfg.get("night_price") or 13)
    night_price_pass = int(cfg.get("night_price_pass") or 12)
    night_cap = int(cfg.get("night_cap") or 69)
    night_cap_pass = int(cfg.get("night_cap_pass") or 59)
    cap_24h = int(cfg.get("cap_24h") or 99)
    cap_24h_pass = int(cfg.get("cap_24h_pass") or 88)
    day_start = str(cfg.get("day_start") or "11:30")
    day_end = str(cfg.get("day_end") or "00:00")
    night_start = str(cfg.get("night_start") or "00:00")
    night_end = str(cfg.get("night_end") or "11:30")

    lines = [
        "--- 新宿定价表 ---",
        f"【白天】{day_start} - {day_end}",
        f"  普通用户：{_money(day_price)} {currency}/小时，封顶 {_money(day_cap)} {currency}",
        f"  月卡用户：{_money(day_price_pass)} {currency}/小时，封顶 {_money(day_cap_pass)} {currency}",
        f"【夜晚】{night_start} - {night_end}",
        f"  普通用户：{_money(night_price)} {currency}/小时，封顶 {_money(night_cap)} {currency}",
        f"  月卡用户：{_money(night_price_pass)} {currency}/小时，封顶 {_money(night_cap_pass)} {currency}",
        f"【连续 24 小时】封顶 {_money(cap_24h)} {currency}（月卡 {_money(cap_24h_pass)} {currency}）",
    ]
    return "\n".join(lines)


def _format_billing(res: dict[str, Any], currency: str) -> str:
    billing = res["billing"]
    session = res["session"]
    discount = res.get("discount")
    original_cost = discount["originalCost"] if discount else billing["totalCost"]
    final_cost = discount["finalCost"] if discount else billing["totalCost"]
    if session.get("costOverwrite") is not None:
        final_cost = session["costOverwrite"]

    total_minutes = int((billing["endTime"] - session["createdAt"]).total_seconds() // 60)
    current_balance = res["wallet"]["total"]["available"]
    lines = [
        "--- 账单详情 ---",
        f"入场: {_dt(session['createdAt'])}",
        f"结算: {_dt(billing['endTime'])}",
        f"时长: {_duration(total_minutes)}",
        "---",
        f"计费价: {_money(original_cost)} {currency}",
    ]
    if discount and discount.get("appliedLogs"):
        for item in discount["appliedLogs"]:
            lines.append(f"  -「{item['asset']}」 -{_money(item['saved'])} {currency}")
    lines.extend(
        [
            f"结算价: {_money(final_cost)} {currency}",
            "---",
            f"当前余额: {_money(current_balance)} {currency}",
            f"扣款后: {_money(current_balance - final_cost)} {currency}",
            "---",
            "计费区间:",
        ]
    )
    if billing["segments"]:
        for segment in billing["segments"]:
            lines.extend(
                [
                    f"- {segment['ruleName']}",
                    f"  时段: {_format_time_range(segment['startTime'], segment['endTime'])}",
                    f"  时长: {_duration(segment['durationMinutes'])}",
                    f"  费用: {_money(segment['cost'])} {currency}{' (已封顶)' if segment['isCapped'] else ''}",
                ]
            )
    else:
        lines.append("  (无)")

    passes = res["wallet"].get("passes", {}).get("details", {}).get("available", [])
    if passes and passes[0].get("expireAt"):
        lines.extend(["---", f"您的月卡将于 {_dt(passes[0]['expireAt'])} 到期。"])
    return "\n".join(lines)


def _format_leave_billing(res: dict[str, Any], currency: str, user_label: str) -> str:
    billing = res["billing"]
    session = res["session"]
    discount = res.get("discount")
    original_cost = discount["originalCost"] if discount else billing["totalCost"]
    final_cost = discount["finalCost"] if discount else billing["totalCost"]
    if session.get("costOverwrite") is not None:
        final_cost = session["costOverwrite"]

    wallet_before = res.get("walletBefore") or res["wallet"]
    wallet_after = res.get("walletAfter")
    balance_before = wallet_before["total"]["available"]
    balance_after = wallet_after["total"]["available"] if wallet_after else balance_before - final_cost
    total_minutes = int((billing["endTime"] - session["createdAt"]).total_seconds() // 60)

    lines = [
        f"✅ 已为用户 {user_label} 退场",
        "离开时请带走自己生产的垃圾以及手套，并且确认关好房门，否则可能会追究您的责任。",
        "--- 账单详情 ---",
        f"入场: {_dt(session['createdAt'])}",
        f"结束: {_dt(billing['endTime'])}",
        f"时长: {_duration(total_minutes)}",
        "---",
        f"计费价: {_money(original_cost)} {currency}",
    ]
    if balance_after < 0:
        lines.insert(1, f"⚠️ 本次结算后欠费 {_money(-balance_after)} {currency}，请联系主理人补款。")
    if discount and discount.get("appliedLogs"):
        for item in discount["appliedLogs"]:
            lines.append(f"  -「{item['asset']}」 -{_money(item['saved'])} {currency}")
    lines.extend(
        [
            f"结算价: {_money(final_cost)} {currency}",
            "---",
            f"当前余额: {_money(balance_before)} {currency}",
            f"扣款后: {_money(balance_after)} {currency}",
        ]
    )
    points_earned = res.get("pointsEarned")
    if points_earned:
        lines.append(f"🎁 本次游玩获得 {_money(points_earned)} 积分")
    lines.extend(["---", "计费区间:"])
    if billing["segments"]:
        for segment in billing["segments"]:
            lines.extend(
                [
                    f"- {segment['ruleName']}",
                    f"时段: {_format_time_range(segment['startTime'], segment['endTime'])}",
                    f"时长: {_duration(segment['durationMinutes'])}",
                    f"费用: {_money(segment['cost'])} {currency}{' (已封顶)' if segment['isCapped'] else ''}",
                ]
            )
    else:
        lines.append("  (无)")
    return "\n".join(lines)


def _format_items(assets: list[dict[str, Any]], currency: str) -> str:
    if not assets:
        return "暂无资产。"
    lines = ["--- 资产 ---"]
    for item in assets:
        asset = item.get("asset") or {}
        name = asset.get("name") or f"{item.get('assetType')}:{item.get('assetDefId')}"
        asset_type = item.get("assetType")
        if asset_type == "CURRENCY":
            suffix = currency
        elif asset_type == "POINTS":
            suffix = "积分"
        else:
            suffix = "个"
        lines.append(
            f"[{item['id']}] {name} x{_money(item['count'])} {suffix}"
            f"｜生效: {_dt(item.get('activeAt'))}｜过期: {_dt(item.get('expireAt'))}"
        )
    return "\n".join(lines)


def _format_history(sessions: list[dict[str, Any]], currency: str) -> str:
    if not sessions:
        return "暂无历史记录。"
    lines = ["--- 历史记录 ---"]
    for item in sessions:
        start = item.get("createdAt")
        end = item.get("closedAt")
        cost = item.get("finalCost")
        active = "进行中" if item.get("isActive") else "已结束"
        lines.append(f"[{item['id']}] {active}｜{_dt(start)} -> {_dt(end) if end else '现在'}｜{_money(cost)} {currency}")
    return "\n".join(lines)


def _format_players(users: list[dict[str, Any]], nicknames: dict[str, str] | None = None) -> str:
    nicknames = nicknames or {}
    lines = [f"👥 店内目前共有 {len(users)} 人"]
    for user in users:
        qq = ""
        for bind in user.get("binds", []):
            if bind.get("type") == "QQ":
                qq = str(bind.get("bid") or "")
                break
        name = nicknames.get(qq) or qq or f"用户#{user['id']}"
        session = (user.get("sessions") or [{}])[0]
        lines.extend(
            [
                "",
                f"玩家: {name}",
                f"入场时间: {_dt(session.get('createdAt'))}",
            ]
        )
    return "\n".join(lines)


@register("astrbot_plugin_shinjuku", "li", "新宿 上机计费插件", "0.1.4")
class ShinjukuPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.currency = str(config.get("currency", "猫粮") or "猫粮")
        try:
            # AstrBot 官方插件数据目录：AstrBot/data/plugin_data/astrbot_plugin_shinjuku/
            default_db = str(StarTools.get_data_dir("astrbot_plugin_shinjuku") / "shinjuku.db")
        except Exception:
            # 旧版 AstrBot 无此接口时回退到插件目录
            default_db = path.join(path.dirname(path.abspath(__file__)), "data", "shinjuku.db")
        db_path = str(config.get("database_path", "") or "") or default_db
        points_per_amount = int(config.get("points_per_amount") or 10)
        self.service = ShinjukuService(
            db_path, self.currency, config.get("billing", {}) or {}, points_per_amount
        )
        self.nicknames: dict[str, str] = {}

    async def terminate(self):
        await self.service.close()

    def _sender_id(self, event: AstrMessageEvent) -> str:
        return str(event.get_sender_id())

    def _sender_uid(self, event: AstrMessageEvent) -> str:
        return f"QQ:{self._sender_id(event)}"

    def _remember_sender_name(self, event: AstrMessageEvent) -> None:
        qq = self._sender_id(event)
        for name in ("get_sender_name", "get_sender_nickname", "get_sender_display_name"):
            method = getattr(event, name, None)
            if callable(method):
                try:
                    value = method()
                except Exception:
                    value = None
                if value:
                    self.nicknames[qq] = str(value)
                    return
        for holder_name in ("sender", "message_obj"):
            holder = getattr(event, holder_name, None)
            if not holder:
                continue
            for attr in ("nickname", "nick", "name", "card", "display_name"):
                value = getattr(holder, attr, None)
                if value:
                    self.nicknames[qq] = str(value)
                    return

    def _admins(self) -> set[str]:
        return {str(item) for item in (self.config.get("admins", []) or [])}

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        return self._sender_id(event) in self._admins()

    def _args(self, event: AstrMessageEvent) -> list[str]:
        text = event.message_str.strip()
        if not text:
            return []
        try:
            parts = shlex.split(text)
        except ValueError:
            parts = text.split()
        if not parts:
            return []
        command_names = {
            "register", "login", "logout", "list", "wallet", "history", "ahistory",
            "billing", "items", "redeem", "add", "mj", "member", "coupon", "giftcode", "j", "入场", "上机", "出场",
            "下机", "离场", "退场", "历史记录", "账单", "b",
        }
        command = parts[0].lstrip("/")
        command = command.split("@", 1)[0]
        return parts[1:] if command in command_names else parts

    def _at_ids(self, event: AstrMessageEvent) -> list[str]:
        ids: list[str] = []
        try:
            components = event.get_messages()
        except Exception:
            components = []
        for component in components:
            kind = f"{type(component).__name__} {getattr(component, 'type', '')}".lower()
            if "at" not in kind and "mention" not in kind:
                continue
            for attr in ("qq", "user_id", "target", "id"):
                value = getattr(component, attr, None)
                if value:
                    ids.append(str(value))
                    break
        return ids

    def _at_label(self, event: AstrMessageEvent, uid: str) -> str:
        qq = uid.split(":", 1)[1] if uid.startswith("QQ:") else uid
        try:
            components = event.get_messages()
        except Exception:
            components = []
        for component in components:
            kind = f"{type(component).__name__} {getattr(component, 'type', '')}".lower()
            if "at" not in kind and "mention" not in kind:
                continue
            component_id = None
            for attr in ("qq", "user_id", "target", "id"):
                value = getattr(component, attr, None)
                if value:
                    component_id = str(value)
                    break
            if component_id != qq:
                continue
            for attr in ("name", "nickname", "nick", "display_name", "display"):
                value = getattr(component, attr, None)
                if value:
                    self.nicknames[qq] = str(value)
                    return f"{value} ({qq})"
        remembered = self.nicknames.get(qq)
        if remembered:
            return f"{remembered} ({qq})"
        return qq

    def _normalize_user(self, raw: str | None, event: AstrMessageEvent, allow_self: bool = True) -> str:
        if not raw:
            if allow_self:
                return self._sender_uid(event)
            raise ShinjukuError("请指定用户。")
        if raw.startswith("QQ:"):
            return raw
        match = re.search(r"\d+", raw)
        if match:
            return f"QQ:{match.group(0)}"
        raise ShinjukuError("无法识别用户，请使用 @用户 或 QQ 号。")

    def _qq_from_uid(self, uid: str) -> str:
        if not uid.startswith("QQ:"):
            raise ShinjukuError("只能自动注册 QQ 用户。")
        return uid.split(":", 1)[1]

    async def _ensure_registered(self, uid: str, user_label: str | None = None) -> str:
        if await self.service.find_user(uid):
            return ""
        qq = self._qq_from_uid(uid)
        register_code = str(self.config.get("redeem_code_on_register", "") or "")
        result = await self.service.register(qq, register_code)
        label = user_label or qq
        if result["created"]:
            prefix = (
                f"用户不存在，尝试注册\n为用户 {label} 注册成功\n\n"
                if user_label
                else "用户不存在，尝试注册\n注册成功\n\n"
            )
            if result.get("gift_error"):
                prefix += f"注册礼包发放失败：{result['gift_error']}\n\n"
            return prefix
        return ""

    def _target_from_optional_arg(self, event: AstrMessageEvent) -> str:
        args = self._args(event)
        if args:
            if not self._is_admin(event):
                raise ShinjukuError("权限不足。")
            return self._normalize_user(args[0], event)
        return self._sender_uid(event)

    async def _safe(self, coro):
        try:
            return await coro
        except ShinjukuError as exc:
            return f"操作失败：{exc.message}"
        except sqlite3.Error as exc:
            logger.error(f"新宿数据库错误: {exc}")
            return f"数据库错误：{exc.__class__.__name__}"
        except Exception as exc:
            logger.error(f"新宿未处理错误: {exc}")
            return f"操作失败：{exc}"

    @filter.command("register")
    async def register_cmd(self, event: AstrMessageEvent):
        """注册新宿用户"""
        self._remember_sender_name(event)
        async def run():
            args = self._args(event)
            if args:
                if not self._is_admin(event):
                    raise ShinjukuError("权限不足。")
                uid = self._normalize_user(args[0], event)
                qq = uid.split(":", 1)[1]
            else:
                qq = self._sender_id(event)
            register_code = str(self.config.get("redeem_code_on_register", "") or "")
            result = await self.service.register(qq, register_code)
            if result["created"]:
                message = f"注册成功，用户 ID：{result['user']['id']}"
                if result.get("gift_error"):
                    message += f"（礼包发放失败：{result['gift_error']}）"
                return message
            return f"已经注册过了，用户 ID：{result['user']['id']}"

        yield event.plain_result(await self._safe(run()))

    @filter.command("login", alias={"入场", "上机"})
    async def login_cmd(self, event: AstrMessageEvent):
        """登录/入场"""
        self._remember_sender_name(event)
        async def run():
            uid = self._target_from_optional_arg(event)
            prefix = await self._ensure_registered(uid)
            session = await self.service.login(uid)
            return prefix + "✅ 入场成功"

        yield event.plain_result(await self._safe(run()))

    @filter.command("logout", alias={"出场", "下机", "离场", "退场"})
    async def logout_cmd(self, event: AstrMessageEvent):
        """登出/结算"""
        self._remember_sender_name(event)
        async def run():
            uid = self._target_from_optional_arg(event)
            result = await self.service.logout(uid)
            return _format_leave_billing(result, self.currency, self._at_label(event, uid))

        yield event.plain_result(await self._safe(run()))

    @filter.command("billing", alias={"账单", "b"})
    async def billing_cmd(self, event: AstrMessageEvent):
        """查看当前账单"""
        async def run():
            uid = self._target_from_optional_arg(event)
            result = await self.service.billing(uid)
            return _format_billing(result, self.currency)

        yield event.plain_result(await self._safe(run()))

    @filter.command("wallet", alias={"钱包"})
    async def wallet_cmd(self, event: AstrMessageEvent):
        """查看钱包"""
        async def run():
            uid = self._target_from_optional_arg(event)
            wallet = await self.service.wallet(uid, False)
            return _format_wallet(wallet, self.currency)

        yield event.plain_result(await self._safe(run()))

    @filter.command("items", alias={"背包"})
    async def items_cmd(self, event: AstrMessageEvent):
        """查看资产"""
        async def run():
            uid = self._target_from_optional_arg(event)
            assets = await self.service.user_assets(uid, True)
            return _format_items(assets, self.currency)

        yield event.plain_result(await self._safe(run()))

    @filter.command("history", alias={"历史记录"})
    async def history_cmd(self, event: AstrMessageEvent):
        """查看自己的历史记录"""
        async def run():
            args = self._args(event)
            limit = int(args[0]) if args and args[0].isdigit() else 5
            sessions = await self.service.history(self._sender_uid(event), limit)
            return _format_history(sessions, self.currency)

        yield event.plain_result(await self._safe(run()))

    @filter.command("ahistory")
    async def ahistory_cmd(self, event: AstrMessageEvent):
        """管理员查看指定用户历史记录"""
        async def run():
            if not self._is_admin(event):
                raise ShinjukuError("权限不足。")
            args = self._args(event)
            if not args:
                raise ShinjukuError("用法：/ahistory <用户> [数量]")
            uid = self._normalize_user(args[0], event, allow_self=False)
            limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else 5
            sessions = await self.service.history(uid, limit)
            return _format_history(sessions, self.currency)

        yield event.plain_result(await self._safe(run()))

    @filter.command("list")
    async def list_cmd(self, event: AstrMessageEvent):
        """列出当前登录用户"""
        async def run():
            if not self._is_admin(event):
                raise ShinjukuError("权限不足。")
            users = await self.service.logged_in_users()
            if not users:
                return "当前没有登录用户。"
            lines = ["--- 当前登录 ---"]
            for user in users:
                binds = ", ".join(f"{bind['type']}:{bind['bid']}" for bind in user.get("binds", []))
                lines.append(f"#{user['id']} {binds or '(无绑定)'}")
            return "\n".join(lines)

        yield event.plain_result(await self._safe(run()))

    @filter.regex(r"^j$")
    async def j_cmd(self, event: AstrMessageEvent):
        """查询当前店内人数"""
        self._remember_sender_name(event)
        async def run():
            users = await self.service.logged_in_users()
            return _format_players(users, self.nicknames)

        yield event.plain_result(await self._safe(run()))

    @filter.regex(r"^定价表$")
    async def pricing_table_cmd(self, event: AstrMessageEvent):
        """发送当前定价表"""
        yield event.plain_result(_format_pricing(self.service.billing_config, self.currency))

    @filter.command("redeem")
    async def redeem_cmd(self, event: AstrMessageEvent):
        """兑换已有兑换码"""
        async def run():
            args = self._args(event)
            if not args:
                raise ShinjukuError("用法：/redeem <兑换码>")
            result = await self.service.redeem(self._sender_uid(event), args[0])
            present = result.get("present") or {}
            assets = result.get("assets") or []
            lines = [f"兑换成功：{present.get('name') or args[0]}"]
            if assets:
                lines.append(f"已发放 {len(assets)} 项资产，可用 /items 查看。")
            return "\n".join(lines)

        yield event.plain_result(await self._safe(run()))

    @filter.command("add")
    async def add_cmd(self, event: AstrMessageEvent):
        """管理员给用户添加货币：/add @用户 金额"""
        async def run():
            if not self._is_admin(event):
                raise ShinjukuError("权限不足。")
            args = self._args(event)
            at_ids = self._at_ids(event)
            if len(args) == 1 and at_ids:
                uid = f"QQ:{at_ids[0]}"
                amount = float(args[0])
            elif len(args) >= 2:
                uid = f"QQ:{at_ids[0]}" if at_ids else self._normalize_user(args[0], event, allow_self=False)
                amount = float(args[-1])
            else:
                raise ShinjukuError("用法：/add @用户 金额")
            if amount <= 0:
                raise ShinjukuError("添加金额必须大于 0。")
            prefix = await self._ensure_registered(uid, self._at_label(event, uid))
            result = await self.service.add_paid_currency(uid, amount, f"admin add by {self._sender_id(event)}")
            return (
                prefix +
                f"为用户 {self._at_label(event, uid)} 增加{self.currency}成功\n"
                f"增加前: {_money(result['originalBalance'])}\n"
                f"增加后: {_money(result['finalBalance'])}"
            )

        yield event.plain_result(await self._safe(run()))

    @filter.command("member")
    async def member_cmd(self, event: AstrMessageEvent):
        """管理员给群成员发放 30 天通行证：/member @成员"""
        async def run():
            if not self._is_admin(event):
                raise ShinjukuError("权限不足。")
            args = self._args(event)
            at_ids = self._at_ids(event)
            if at_ids:
                uid = f"QQ:{at_ids[0]}"
            elif args:
                uid = self._normalize_user(args[0], event, allow_self=False)
            else:
                raise ShinjukuError("用法：/member @成员")
            prefix = await self._ensure_registered(uid, self._at_label(event, uid))
            result = await self.service.add_pass(uid, 30, f"member grant by {self._sender_id(event)}")
            return (
                prefix +
                f"已为用户 {self._at_label(event, uid)} 发放 30 天通行证\n"
                f"到期时间: {_dt(result.get('expireAt'))}"
            )

        yield event.plain_result(await self._safe(run()))

    @filter.command("coupon")
    async def coupon_cmd(self, event: AstrMessageEvent):
        """管理员发放折扣优惠券：/coupon @用户 8 [天数]（8 表示 8 折，默认 30 天）"""
        async def run():
            if not self._is_admin(event):
                raise ShinjukuError("权限不足。")
            args = self._args(event)
            at_ids = self._at_ids(event)
            if at_ids:
                if not args:
                    raise ShinjukuError("用法：/coupon @用户 折扣 [天数]")
                uid = f"QQ:{at_ids[0]}"
                nums = [arg for arg in args if not arg.startswith("@")]
                if not nums:
                    raise ShinjukuError("用法：/coupon @用户 折扣 [天数]")
                tenths = float(nums[0])
                days = int(nums[1]) if len(nums) > 1 else 30
            elif len(args) >= 2:
                uid = self._normalize_user(args[0], event, allow_self=False)
                tenths = float(args[1])
                days = int(args[2]) if len(args) > 2 else 30
            else:
                raise ShinjukuError("用法：/coupon @用户 折扣 [天数]")
            prefix = await self._ensure_registered(uid, self._at_label(event, uid))
            result = await self.service.grant_coupon(uid, tenths, days, f"coupon grant by {self._sender_id(event)}")
            label = result["asset"].get("name") or f"{result['discount_tenths']:g}折优惠券"
            return (
                prefix +
                f"已为用户 {self._at_label(event, uid)} 发放 {label}\n"
                f"有效期至: {_dt(result['userAsset'].get('expireAt'))}"
            )

        yield event.plain_result(await self._safe(run()))

    @filter.command("giftcode")
    async def giftcode_cmd(self, event: AstrMessageEvent):
        """管理员生成兑换码：/giftcode 礼包ID 货币数量 次数"""
        async def run():
            if not self._is_admin(event):
                raise ShinjukuError("权限不足。")
            args = self._args(event)
            if len(args) != 3:
                raise ShinjukuError("用法：/giftcode 礼包ID 货币数量 次数")
            try:
                present_id = int(args[0])
                amount = float(args[1])
                times = int(args[2])
            except ValueError:
                raise ShinjukuError("用法：/giftcode 礼包ID 货币数量 次数")
            result = await self.service.create_gift_code(present_id, amount, times)
            return (
                f"已生成兑换码：{result['code']}\n"
                f"礼包：{result['name']}（含 {_money(result['currency_amount'])} {self.currency}）\n"
                f"可领取次数：{result['max_use_count']}\n"
                "发送 /redeem <兑换码> 即可领取"
            )

        yield event.plain_result(await self._safe(run()))

    @filter.command("mj")
    async def mj_cmd(self, event: AstrMessageEvent):
        """管理员扣除用户货币：/mj @用户 金额"""
        async def run():
            if not self._is_admin(event):
                raise ShinjukuError("权限不足。")
            args = self._args(event)
            at_ids = self._at_ids(event)
            if len(args) == 1 and at_ids:
                uid = f"QQ:{at_ids[0]}"
                amount = float(args[0])
            elif len(args) >= 2:
                uid = f"QQ:{at_ids[0]}" if at_ids else self._normalize_user(args[0], event, allow_self=False)
                amount = float(args[-1])
            else:
                raise ShinjukuError("用法：/mj @用户 金额")
            if amount <= 0:
                raise ShinjukuError("扣费金额必须大于 0。")
            result = await self.service.charge_wallet(uid, amount, f"mj charge by {self._sender_id(event)}")
            return (
                f"MJ 扣费成功：-{_money(amount)} {self.currency}\n"
                f"余额：{_money(result['originalBalance'])} -> {_money(result['finalBalance'])} {self.currency}"
            )

        yield event.plain_result(await self._safe(run()))
