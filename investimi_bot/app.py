from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from telegram import (
    BotCommand,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonCommands,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from investimi_bot.config import AlertsConfig, load_alerts_config
from investimi_bot.providers.fred import FredClient
from investimi_bot.providers.fmp import FmpClient
from investimi_bot.providers.prices import StooqClient
from investimi_bot.providers.congress_trades_free import CongressTradesFreeClient
from investimi_bot.providers.sec_edgar import SecEdgarClient
from investimi_bot.providers.twelve_data import TwelveDataClient
from investimi_bot.rules import RuleEngine
from investimi_bot.settings import load_settings
from investimi_bot.state import StateStore


def _render_template(text: str, ctx: dict[str, Any]) -> str:
    # template minimale: {{key}} con supporto dot path tipo {{left.value}}
    out = text
    for _ in range(50):  # evita loop infinito se qualcosa va storto
        start = out.find("{{")
        if start < 0:
            break
        end = out.find("}}", start + 2)
        if end < 0:
            break
        expr = out[start + 2 : end].strip()
        val: Any = ctx
        for part in expr.split("."):
            if isinstance(val, dict) and part in val:
                val = val[part]
            else:
                val = ""
                break
        out = out[:start] + str(val) + out[end + 2 :]
    return out


class BotApp:
    def __init__(
        self,
        config: AlertsConfig,
        state: StateStore,
        engine: RuleEngine,
        *,
        telegram_channel_id: int | None,
    ) -> None:
        self._config = config
        self._state_store = state
        self._engine = engine
        self._telegram_channel_id = telegram_channel_id
        self._state = self._state_store.load()

        self._state.setdefault("subscribers", [])  # chat_id list
        self._state.setdefault("rule_last_fingerprint", {})  # id -> fingerprint
        self._state.setdefault("rule_last_error", {})  # id -> last error string

        # Prune state for rules that no longer exist in config
        rule_ids = {r.id for r in self._config.rules}
        fp = self._state.get("rule_last_fingerprint") or {}
        err = self._state.get("rule_last_error") or {}
        new_fp = {k: v for k, v in fp.items() if k in rule_ids}
        new_err = {k: v for k, v in err.items() if k in rule_ids}
        if new_fp != fp or new_err != err:
            self._state["rule_last_fingerprint"] = new_fp
            self._state["rule_last_error"] = new_err
            self.save_state()

    def save_state(self) -> None:
        self._state_store.save(self._state)

    def subscribers(self) -> list[int]:
        return list({int(x) for x in self._state.get("subscribers", [])})

    def add_subscriber(self, chat_id: int) -> None:
        subs = set(self.subscribers())
        subs.add(chat_id)
        self._state["subscribers"] = sorted(subs)
        self.save_state()

    def remove_subscriber(self, chat_id: int) -> None:
        subs = set(self.subscribers())
        subs.discard(chat_id)
        self._state["subscribers"] = sorted(subs)
        self.save_state()

    def should_notify(self, rule_id: str, fingerprint: str) -> bool:
        last = (self._state.get("rule_last_fingerprint") or {}).get(rule_id)
        if last == fingerprint:
            return False
        self._state["rule_last_fingerprint"][rule_id] = fingerprint
        self.save_state()
        return True

    async def run_rule(self, rule_id: str, app: Application) -> None:
        rule = next((r for r in self._config.rules if r.id == rule_id), None)
        if not rule:
            return

        try:
            result = self._engine.eval(rule.when)
        except Exception as e:
            # errore silenzioso ma visibile con /status
            msg = str(e)
            # redazione semplice: evita di salvare apikey in chiaro
            if "apikey=" in msg:
                msg = msg.split("apikey=")[0] + "apikey=***"
            self._state.setdefault("rule_last_error", {})[rule_id] = msg
            self.save_state()
            return

        self._state.setdefault("rule_last_error", {}).pop(rule_id, None)
        self.save_state()

        if not result.ok:
            return

        if not self.should_notify(rule_id, result.fingerprint):
            return

        msg = _render_template(rule.notify.text, result.context)
        # If a broadcast channel is configured, prefer sending there.
        if self._telegram_channel_id is not None:
            await app.bot.send_message(chat_id=int(self._telegram_channel_id), text=msg)
            return

        for chat_id in self.subscribers():
            try:
                await app.bot.send_message(chat_id=chat_id, text=msg)
            except Exception:
                continue

    async def ensure_channel_panel(self, app: Application) -> None:
        """
        Create (or refresh) a pinned "control panel" message in the channel.
        This is the closest thing to "fixed buttons" in Telegram channels.
        """
        if self._telegram_channel_id is None:
            return
        channel_id = int(self._telegram_channel_id)

        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🕑 Ultime 24h", callback_data="insider:24h")],
                [InlineKeyboardButton("📅 Top settimana (valore)", callback_data="insider:7d")],
                [InlineKeyboardButton("🗓️ Top mese (valore)", callback_data="insider:30d")],
            ]
        )
        text = (
            "📌 *Investimi — Insider Panel*\n\n"
            "Usa i pulsanti qui sotto per generare i report."
        )

        panel = (self._state.get("channel_panel") or {}) if isinstance(self._state.get("channel_panel"), dict) else {}
        msg_id = panel.get("message_id")

        try:
            if isinstance(msg_id, int) and msg_id > 0:
                # Try to edit existing panel (keeps it at the top if pinned)
                await app.bot.edit_message_text(
                    chat_id=channel_id,
                    message_id=msg_id,
                    text=text,
                    reply_markup=kb,
                    parse_mode="Markdown",
                )
            else:
                sent = await app.bot.send_message(
                    chat_id=channel_id,
                    text=text,
                    reply_markup=kb,
                    parse_mode="Markdown",
                )
                self._state["channel_panel"] = {"message_id": int(sent.message_id)}
                self.save_state()
                # Best-effort pin (requires admin + can_pin_messages)
                try:
                    await app.bot.pin_chat_message(chat_id=channel_id, message_id=int(sent.message_id), disable_notification=True)
                except Exception:
                    pass
        except Exception:
            # If edit failed (deleted/permissions), re-create
            sent = await app.bot.send_message(
                chat_id=channel_id,
                text=text,
                reply_markup=kb,
                parse_mode="Markdown",
            )
            self._state["channel_panel"] = {"message_id": int(sent.message_id)}
            self.save_state()
            try:
                await app.bot.pin_chat_message(chat_id=channel_id, message_id=int(sent.message_id), disable_notification=True)
            except Exception:
                pass


def _main_reply_keyboard() -> ReplyKeyboardMarkup:
    """
    "Fixed buttons" under the input field in bot chats.
    (This is NOT an inline keyboard; it stays visible like in many bots.)
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton("🕑 Ultime 24h"),
                KeyboardButton("📅 Top settimana"),
                KeyboardButton("🗓️ Top mese"),
            ],
            [KeyboardButton("📌 Stato"), KeyboardButton("📜 Regole")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Scegli un report…",
    )


def _parse_date_yyyy_mm_dd(s: str | None) -> "datetime | None":
    from datetime import datetime

    if not s:
        return None
    try:
        y, m, d = (int(x) for x in s.strip().split("-"))
        return datetime(y, m, d)
    except Exception:
        return None


def _parse_date_mm_dd_yyyy(s: str | None) -> "datetime | None":
    from datetime import datetime

    if not s:
        return None
    s = s.strip()
    try:
        mm, dd, yy = (int(x) for x in s.split("/"))
        return datetime(yy, mm, dd)
    except Exception:
        return None


def _build_insider_report(
    engine: RuleEngine,
    config: AlertsConfig,
    *,
    cutoff: "datetime",
    within_days_sec: int,
    label: str,
) -> str:
    def _fmt_money(v: float | None) -> str:
        if v is None:
            return "—"
        try:
            v = float(v)
        except Exception:
            return "—"
        if abs(v) >= 1_000_000_000:
            return f"${v/1_000_000_000:.2f}B"
        if abs(v) >= 1_000_000:
            return f"${v/1_000_000:.2f}M"
        if abs(v) >= 1_000:
            return f"${v/1_000:.1f}k"
        return f"${v:.0f}"

    def _fmt_num(v: float | None) -> str:
        if v is None:
            return "—"
        try:
            v = float(v)
        except Exception:
            return "—"
        if abs(v) >= 1_000_000:
            return f"{v:,.0f}"
        if abs(v) >= 1_000:
            return f"{v:,.0f}"
        return f"{v:.2f}".rstrip("0").rstrip(".")

    def _side_emoji(side: str | None) -> str:
        s = (side or "").lower()
        if s in ("acquire", "buy", "purchase"):
            return "🟢"
        if s in ("dispose", "sell", "sale"):
            return "🔴"
        return "⚪️"

    lines: list[str] = [f"📌 Report insider — {label}"]

    # PRIVATI (SEC Form 4)
    priv_lines: list[str] = []
    if engine._sec:  # noqa: SLF001
        sec_rule = next((r for r in config.rules if r.id == "insider_buys_watchlist"), None)
        tickers = (sec_rule.when.tickers if sec_rule and hasattr(sec_rule.when, "tickers") else [])  # type: ignore[attr-defined]
        txs = []
        for t in tickers:
            txs.extend(engine._sec.form4_transactions(t, within_days=within_days_sec))  # noqa: SLF001
        items = []
        for tx in txs:
            dt = _parse_date_yyyy_mm_dd(tx.filed_at)
            if not dt or dt < cutoff:
                continue
            items.append(tx)
        items_sorted = sorted(items, key=lambda x: (x.value_usd or 0.0), reverse=True)[:10]
        if items_sorted:
            priv_lines.append("\n🕵️ Privati (SEC Form 4)")
            for tx in items_sorted:
                who = tx.reporting_owner or "(unknown)"
                title = tx.owner_title or ""
                side = tx.side or ""
                code = tx.code or ""
                priv_lines.append(
                    f"- {_side_emoji(side)} 📌 {tx.ticker} | 💰 {_fmt_money(tx.value_usd)} | 🗓️ {tx.filed_at}\n"
                    f"  👤 {who}{(' — 🪪 ' + title) if title else ''}\n"
                    f"  🔁 {side}{(' (code=' + code + ')') if code else ''} | 🧾 {_fmt_num(tx.shares)} sh | 💲 {_fmt_num(tx.price)}"
                )
        else:
            priv_lines.append("\n🕵️ Privati (SEC Form 4)\n- (nessun evento nel periodo)")
    else:
        priv_lines.append("\n🕵️ Privati (SEC Form 4)\n- (SEC_USER_AGENT non configurato)")

    # POLITICI (STOCK Act)
    pol_lines: list[str] = []
    if engine._congress:  # noqa: SLF001
        cong_rule = next((r for r in config.rules if r.id == "us_congress_trades_50k_watch"), None)
        symbols = (cong_rule.when.symbols if cong_rule and hasattr(cong_rule.when, "symbols") else [])  # type: ignore[attr-defined]
        symset = {s.upper() for s in symbols} if symbols else None
        trades = []
        for chamber in ["senate", "house"]:
            trades.extend(engine._congress.fetch(chamber))  # type: ignore[arg-type]  # noqa: SLF001
        items = []
        for t in trades:
            dt = _parse_date_mm_dd_yyyy(t.transaction_date)
            if not dt or dt < cutoff:
                continue
            if symset and (t.ticker or "").upper() not in symset:
                continue
            items.append(t)
        items_sorted = sorted(items, key=lambda x: (x.amount_max_usd or x.amount_min_usd or 0.0), reverse=True)[:10]
        if items_sorted:
            pol_lines.append("\n🏛️ Politici (STOCK Act)")
            for t in items_sorted:
                amt = t.amount_max_usd or t.amount_min_usd
                pol_lines.append(
                    f"- {_side_emoji(t.side)} 📌 {t.ticker or '—'} | 💸 {t.amount_raw or '—'} | 🗓️ {t.transaction_date or '—'}\n"
                    f"  👤 {t.politician or '—'} | 🏛️ {t.chamber}\n"
                    f"  🔁 {t.side} ({t.transaction_type or '—'}) | 📈 max {_fmt_money(amt)}"
                )
        else:
            # help diagnose: show data freshness
            latest_dt = None
            for t in trades:
                dt = _parse_date_mm_dd_yyyy(t.transaction_date)
                if dt and (latest_dt is None or dt > latest_dt):
                    latest_dt = dt
            suffix = ""
            if latest_dt:
                suffix = f" (dataset sembra fermo a ~{latest_dt.date().isoformat()})"
            pol_lines.append(f"\n🏛️ Politici (STOCK Act)\n- (nessun evento nel periodo){suffix}")
    else:
        pol_lines.append("\n🏛️ Politici (STOCK Act)\n- (CONGRESS_TRADES_USER_AGENT non configurato)")

    return "\n".join(lines + priv_lines + pol_lines)


def main() -> int:
    settings = load_settings()

    config_path = Path("config/alerts.yaml")
    config = load_alerts_config(config_path)

    state = StateStore(Path("state/state.json"))

    engine = RuleEngine(
        fred=FredClient(api_key=settings.fred_api_key),
        prices=StooqClient(),
        fmp=FmpClient(settings.fmp_api_key) if settings.fmp_api_key else None,
        twelve_data=TwelveDataClient(settings.twelve_data_api_key) if settings.twelve_data_api_key else None,
        sec=SecEdgarClient(settings.sec_user_agent) if settings.sec_user_agent else None,
        congress=(
            CongressTradesFreeClient(
                user_agent=settings.congress_trades_user_agent,
                senate_url=settings.congress_senate_url
                or "https://raw.githubusercontent.com/timothycarambat/senate-stock-watcher-data/master/aggregate/all_transactions_for_senators.json",
                house_url=settings.congress_house_url,
            )
            if settings.congress_trades_user_agent
            else None
        ),
    )
    bot_app = BotApp(
        config=config,
        state=state,
        engine=engine,
        telegram_channel_id=settings.telegram_channel_id,
    )

    async def _setup_bot_menu(app: Application) -> None:
        # This enables the fixed "Menu" button in bot chats with a command list.
        commands = [
            BotCommand("insider24", "Report insider ultime 24h"),
            BotCommand("insider7", "Top insider settimana (valore)"),
            BotCommand("insider30", "Top insider mese (valore)"),
            BotCommand("status", "Stato bot / errori"),
            BotCommand("rules", "Lista regole attive"),
            BotCommand("run", "Esegui subito una regola: /run <rule_id>"),
            BotCommand("test", "Ping"),
        ]
        await app.bot.set_my_commands(commands, scope=BotCommandScopeDefault())
        # Note: Menu button is only visible in private chats with the bot.
        await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())

    async def _post_init(app: Application) -> None:
        # Runs once on startup inside PTB lifecycle.
        try:
            await _setup_bot_menu(app)
        except Exception:
            # Don't crash bot if Telegram rejects menu calls.
            return

    application = Application.builder().token(settings.telegram_bot_token).post_init(_post_init).build()

    async def _is_allowed(update: Update) -> bool:
        if not update.effective_chat:
            return False
        if settings.telegram_allowed_chat_ids is None:
            return True
        return int(update.effective_chat.id) in settings.telegram_allowed_chat_ids

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _is_allowed(update):
            return
        chat = update.effective_chat
        assert chat is not None
        if settings.telegram_channel_id is None:
            bot_app.add_subscriber(int(chat.id))
            await update.message.reply_text(
                "Ok! Ti ho iscritto alle notifiche.\n"
                "Comandi: /status /rules /stop /test\n\n"
                "⬇️ Hai i pulsanti fissi qui sotto (come in bnb).",
                reply_markup=_main_reply_keyboard(),
            )
        else:
            await update.message.reply_text(
                "Ok! Modalità canale attiva: le notifiche verranno pubblicate nel canale.\n"
                "Comandi: /status /rules /test"
            )

    async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _is_allowed(update):
            return
        chat = update.effective_chat
        assert chat is not None
        if settings.telegram_channel_id is None:
            bot_app.remove_subscriber(int(chat.id))
            await update.message.reply_text("Ok, notifiche disattivate per questa chat.")
        else:
            await update.message.reply_text("Modalità canale attiva: /stop non è necessario.")

    async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _is_allowed(update):
            return
        subs = bot_app.subscribers()
        errors_all = (bot_app._state.get("rule_last_error") or {})  # noqa: SLF001 (semplice app)
        rule_ids = {r.id for r in config.rules}
        errors = {rid: msg for rid, msg in errors_all.items() if rid in rule_ids}
        err_lines = [f"- {rid}: {msg}" for rid, msg in errors.items()]
        await update.message.reply_text(
            "Status:\n"
            f"- subscribers: {len(subs)}\n"
            f"- rules: {len(config.rules)}\n"
            + ("- errors:\n" + "\n".join(err_lines) if err_lines else "- errors: none")
        )

    async def rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _is_allowed(update):
            return
        lines = []
        for r in config.rules:
            lines.append(f"- {r.id} (every {r.every_seconds}s)")
        await update.message.reply_text("Rules:\n" + ("\n".join(lines) if lines else "(none)"))

    async def test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _is_allowed(update):
            return
        await update.message.reply_text("Test ok. Scheduler attivo.")

    async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _is_allowed(update):
            return
        try:
            await _setup_bot_menu(application)
        except Exception as e:
            await update.message.reply_text(f"❌ Menu non impostato: {e}")
            return
        await update.message.reply_text(
            "✅ Menu impostato.\n"
            "Nota: il bottone 'Menu' si vede solo nella chat privata col bot.\n"
            "Chiudi e riapri la chat se non compare subito."
        )

    async def run_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _is_allowed(update):
            return
        args = getattr(context, "args", None) or []
        if not args:
            await update.message.reply_text("Uso: /run <rule_id>")
            return
        rule_id = str(args[0]).strip()
        rule = next((r for r in config.rules if r.id == rule_id), None)
        if not rule:
            await update.message.reply_text(f"Rule non trovata: {rule_id}")
            return
        try:
            result = engine.eval(rule.when)
        except Exception as e:
            msg = str(e)
            if "apikey=" in msg:
                msg = msg.split("apikey=")[0] + "apikey=***"
            await update.message.reply_text(f"❌ errore: {msg}")
            return

        # always report outcome; if ok, also render notification preview
        count = None
        if isinstance(result.context.get("count"), int):
            count = int(result.context["count"])
        if not result.ok:
            await update.message.reply_text(f"✅ run ok. match=NO (count={count})")
            return
        preview = _render_template(rule.notify.text, result.context)
        await update.message.reply_text(f"✅ run ok. match=YES (count={count})\n\n---preview---\n{preview}")

    async def insider(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _is_allowed(update):
            return
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🕑 Ultime 24h", callback_data="insider:24h")],
                [InlineKeyboardButton("📅 Top settimana (valore)", callback_data="insider:7d")],
                [InlineKeyboardButton("🗓️ Top mese (valore)", callback_data="insider:30d")],
            ]
        )
        await update.message.reply_text(
            "Scegli cosa vuoi vedere (i pulsanti compaiono *sotto* questo messaggio).\n\n"
            "Se non li vedi, usa i comandi:\n"
            "- /insider24\n"
            "- /insider7\n"
            "- /insider30",
            reply_markup=kb,
        )

    async def _insider_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.callback_query:
            return
        if not await _is_allowed(update):
            return
        q = update.callback_query
        await q.answer()
        data = str(q.data or "")
        if not data.startswith("insider:"):
            return
        window = data.split(":", 1)[1]

        from datetime import datetime, timedelta

        now = datetime.utcnow()
        if window == "24h":
            cutoff = now - timedelta(hours=24)
            within_days_sec = 2
            label = "Ultime 24h"
        elif window == "7d":
            cutoff = now - timedelta(days=7)
            within_days_sec = 7
            label = "Top settimana"
        else:
            cutoff = now - timedelta(days=30)
            within_days_sec = 30
            label = "Top mese"

        msg = _build_insider_report(engine, config, cutoff=cutoff, within_days_sec=within_days_sec, label=label)
        # Keep buttons on the message for easy re-run
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🕑 Ultime 24h", callback_data="insider:24h")],
                [InlineKeyboardButton("📅 Top settimana (valore)", callback_data="insider:7d")],
                [InlineKeyboardButton("🗓️ Top mese (valore)", callback_data="insider:30d")],
            ]
        )
        await q.edit_message_text(msg, reply_markup=kb)

    async def insider_24h(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _is_allowed(update):
            return
        from datetime import datetime, timedelta

        now = datetime.utcnow()
        cutoff = now - timedelta(hours=24)
        msg = _build_insider_report(engine, config, cutoff=cutoff, within_days_sec=2, label="Ultime 24h")
        await update.message.reply_text(msg)

    async def insider_7d(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _is_allowed(update):
            return
        from datetime import datetime, timedelta

        now = datetime.utcnow()
        cutoff = now - timedelta(days=7)
        msg = _build_insider_report(engine, config, cutoff=cutoff, within_days_sec=7, label="Top settimana (valore)")
        await update.message.reply_text(msg)

    async def insider_30d(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _is_allowed(update):
            return
        from datetime import datetime, timedelta

        now = datetime.utcnow()
        cutoff = now - timedelta(days=30)
        msg = _build_insider_report(engine, config, cutoff=cutoff, within_days_sec=30, label="Top mese (valore)")
        await update.message.reply_text(msg)

    async def kbtest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _is_allowed(update):
            return
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ INLINE OK", callback_data="insider:24h")]])
        await update.message.reply_text(
            "Test tastiera inline: dovresti vedere 1 bottone sotto questo messaggio.\n"
            "Se non compare, è un limite del client/chat Telegram.",
            reply_markup=kb,
        )

    async def panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _is_allowed(update):
            return
        # In channel-mode, create/pin the panel in the channel.
        if settings.telegram_channel_id is not None:
            await bot_app.ensure_channel_panel(application)
            await update.message.reply_text("Ok: pannello creato/aggiornato nel canale.")
        else:
            await update.message.reply_text("Questo comando serve in modalità canale (TELEGRAM_CHANNEL_ID).")

    async def _fixed_buttons_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _is_allowed(update):
            return
        if not update.message:
            return
        txt = (update.message.text or "").strip()
        if txt == "🕑 Ultime 24h":
            await insider_24h(update, context)
        elif txt == "📅 Top settimana":
            await insider_7d(update, context)
        elif txt == "🗓️ Top mese":
            await insider_30d(update, context)
        elif txt == "📌 Stato":
            await status(update, context)
        elif txt == "📜 Regole":
            await rules_cmd(update, context)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("rules", rules_cmd))
    application.add_handler(CommandHandler("test", test))
    application.add_handler(CommandHandler("menu", menu_cmd))
    application.add_handler(CommandHandler("run", run_now))
    application.add_handler(CommandHandler("insider", insider))
    application.add_handler(CommandHandler("insider24", insider_24h))
    application.add_handler(CommandHandler("insider7", insider_7d))
    application.add_handler(CommandHandler("insider30", insider_30d))
    application.add_handler(CommandHandler("kbtest", kbtest))
    application.add_handler(CommandHandler("panel", panel))
    application.add_handler(CallbackQueryHandler(_insider_callback, pattern=r"^insider:"))
    # Reply keyboard buttons (fixed under input field)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _fixed_buttons_handler))

    async def _job_callback(context: ContextTypes.DEFAULT_TYPE) -> None:
        data = getattr(context.job, "data", None) or {}
        rule_id = str(data.get("rule_id") or "")
        if not rule_id:
            return
        await bot_app.run_rule(rule_id, context.application)

    # Usa il JobQueue nativo di python-telegram-bot (gira nel suo event loop).
    for rule in config.rules:
        application.job_queue.run_repeating(
            _job_callback,
            interval=rule.every_seconds,
            first=5,
            data={"rule_id": rule.id},
            name=f"rule:{rule.id}",
        )

    # In channel mode, publish a pinned control panel at startup.
    if settings.telegram_channel_id is not None:
        async def _panel_startup(context: ContextTypes.DEFAULT_TYPE) -> None:
            await bot_app.ensure_channel_panel(context.application)

        application.job_queue.run_once(_panel_startup, when=3, name="channel_panel_startup")

    application.run_polling(close_loop=False)
    return 0

