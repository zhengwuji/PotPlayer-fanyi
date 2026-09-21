# -*- coding: utf-8 -*-
"""
Jet Hub 现代化管理看板 UI (Jet Hub Management Dialog)
移植自 F:\\源码\\dsh-codearts-auth / plugin-src/client/jet-hub.js
1:1 复刻 Jet Hub 原生多账号管理体验
"""

import os
import sys
import time
import json
import threading
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from jet_hub_service import (
    JetHubAccountManager,
    PROVIDERS_INFO,
    BuddyClient,
    LOCAL_STORE_PATH,
    get_dsh_paths
)


class JetHubDialog(tk.Toplevel):
    """
    Jet Hub 多账号管理面板
    """
    def __init__(self, parent=None, on_update_callback=None):
        super().__init__(parent)
        self.title("Jet Hub - Provider 凭据管理与多账号支持")
        self.geometry("920x680")
        self.minsize(800, 560)
        self.configure(bg="#F8F9FA")

        self.mgr = JetHubAccountManager()
        self.on_update_callback = on_update_callback
        self.current_provider = "buddy"

        # 主题色彩
        self.c_bg = "#F8F9FA"
        self.c_card = "#FFFFFF"
        self.c_border = "#E5E7EB"
        self.c_primary = "#2563EB"
        self.c_primary_light = "#EFF6FF"
        self.c_text_main = "#111827"
        self.c_text_sub = "#6B7280"
        self.c_green = "#10B981"
        self.c_red = "#EF4444"

        self._build_ui()
        self._select_provider("buddy")

        # 居中窗口
        self.update_idletasks()
        w = self.winfo_width()
        h = self.winfo_height()
        ws = self.winfo_screenwidth()
        hs = self.winfo_screenheight()
        x = (ws // 2) - (w // 2)
        y = (hs // 2) - (h // 2) - 30
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _build_ui(self):
        # 1. 顶部 Header
        header_frame = tk.Frame(self, bg=self.c_bg, padx=24, pady=16)
        header_frame.pack(fill=tk.X)

        title_box = tk.Frame(header_frame, bg=self.c_bg)
        title_box.pack(side=tk.LEFT)

        title_lbl = tk.Label(title_box, text="Jet Hub", font=("Microsoft YaHei UI", 16, "bold"), fg=self.c_text_main, bg=self.c_bg)
        title_lbl.pack(anchor=tk.W)

        sub_lbl = tk.Label(title_box, text="Provider 凭据管理与多账号支持 (兼容 DSH 凭据池与自动轮换)", font=("Microsoft YaHei UI", 9), fg=self.c_text_sub, bg=self.c_bg)
        sub_lbl.pack(anchor=tk.W, pady=(2, 0))

        btn_box = tk.Frame(header_frame, bg=self.c_bg)
        btn_box.pack(side=tk.RIGHT)

        open_cfg_btn = tk.Button(
            btn_box, text="打开配置文件", font=("Microsoft YaHei UI", 9),
            bg="#FFFFFF", fg=self.c_text_main, relief=tk.FLAT, bd=1,
            highlightbackground=self.c_border, highlightthickness=1, padx=12, pady=4,
            cursor="hand2", command=self._open_config_file
        )
        open_cfg_btn.pack(side=tk.LEFT, padx=6)

        close_btn = tk.Button(
            btn_box, text="关闭", font=("Microsoft YaHei UI", 9),
            bg="#FFFFFF", fg=self.c_text_main, relief=tk.FLAT, bd=1,
            highlightbackground=self.c_border, highlightthickness=1, padx=12, pady=4,
            cursor="hand2", command=self.destroy
        )
        close_btn.pack(side=tk.LEFT, padx=6)

        # 细分割线
        sep = tk.Frame(self, height=1, bg=self.c_border)
        sep.pack(fill=tk.X)

        # 2. 主体左右分栏
        body_frame = tk.Frame(self, bg=self.c_bg)
        body_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=16)

        # 左侧 Provider 导航菜单
        self.nav_frame = tk.Frame(body_frame, bg=self.c_bg, width=220)
        self.nav_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 16))
        self.nav_frame.pack_propagate(False)

        self.nav_buttons = {}
        providers_order = [
            ("codearts", "CodeArts (华为云)", "🏢"),
            ("buddy", "CodeBuddy (国内版)", "🚀"),
            ("buddy-intl", "CodeBuddy (国际版)", "🌐"),
            ("workbuddy-cn", "WorkBuddy (国内版)", "💼"),
            ("workbuddy", "WorkBuddy (国际版)", "🌍"),
            ("antigravity", "Antigravity (Google)", "⚡"),
        ]

        for p_id, p_name, icon in providers_order:
            btn = tk.Button(
                self.nav_frame, text=f"  {icon}  {p_name}",
                font=("Microsoft YaHei UI", 10), anchor=tk.W,
                bg="#FFFFFF", fg=self.c_text_main, relief=tk.FLAT, bd=1,
                highlightbackground=self.c_border, highlightthickness=1,
                padx=14, pady=10, cursor="hand2",
                command=lambda pid=p_id: self._select_provider(pid)
            )
            btn.pack(fill=tk.X, pady=4)
            self.nav_buttons[p_id] = btn

        # 右侧操作与账号列表
        self.content_frame = tk.Frame(body_frame, bg=self.c_bg)
        self.content_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        # 右侧顶栏：渠道标题与功能按钮
        top_bar = tk.Frame(self.content_frame, bg=self.c_bg)
        top_bar.pack(fill=tk.X, pady=(0, 12))

        self.right_title_lbl = tk.Label(
            top_bar, text="CodeBuddy (国内版) 账号管理",
            font=("Microsoft YaHei UI", 13, "bold"), fg=self.c_text_main, bg=self.c_bg
        )
        self.right_title_lbl.pack(side=tk.LEFT)

        action_box = tk.Frame(top_bar, bg=self.c_bg)
        action_box.pack(side=tk.RIGHT)

        self.claim_btn = tk.Button(
            action_box, text="一键领取", font=("Microsoft YaHei UI", 9),
            bg="#FFFFFF", fg=self.c_text_main, relief=tk.FLAT, bd=1,
            highlightbackground=self.c_border, highlightthickness=1, padx=10, pady=3,
            cursor="hand2", command=self._on_claim_all
        )
        self.claim_btn.pack(side=tk.LEFT, padx=4)

        self.retest_all_btn = tk.Button(
            action_box, text="重测所有", font=("Microsoft YaHei UI", 9),
            bg="#FFFFFF", fg=self.c_text_main, relief=tk.FLAT, bd=1,
            highlightbackground=self.c_border, highlightthickness=1, padx=10, pady=3,
            cursor="hand2", command=self._on_retest_all
        )
        self.retest_all_btn.pack(side=tk.LEFT, padx=4)

        self.reset_all_btn = tk.Button(
            action_box, text="重置所有", font=("Microsoft YaHei UI", 9),
            bg="#FFFFFF", fg=self.c_text_main, relief=tk.FLAT, bd=1,
            highlightbackground=self.c_border, highlightthickness=1, padx=10, pady=3,
            cursor="hand2", command=self._on_reset_all
        )
        self.reset_all_btn.pack(side=tk.LEFT, padx=4)

        self.add_btn = tk.Button(
            action_box, text="+ 新建账号", font=("Microsoft YaHei UI", 9, "bold"),
            bg=self.c_primary, fg="#FFFFFF", relief=tk.FLAT, padx=12, pady=4,
            cursor="hand2", command=self._on_add_account
        )
        self.add_btn.pack(side=tk.LEFT, padx=(4, 0))

        # 账号卡片容器（支持滚动）
        container = tk.Frame(self.content_frame, bg=self.c_bg)
        container.pack(fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(container, bg=self.c_bg, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(container, orient="vertical", command=self.canvas.yview)
        self.card_list_frame = tk.Frame(self.canvas, bg=self.c_bg)

        self.card_list_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        self.canvas_window = self.canvas.create_window((0, 0), window=self.card_list_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.canvas.bind("<Configure>", self._on_canvas_configure)

        # 底部状态栏
        self.status_bar = tk.Label(
            self, text="就绪 | 已自动载入本地 DSH 账号与凭据池",
            font=("Microsoft YaHei UI", 8), fg=self.c_text_sub, bg="#F3F4F6", anchor=tk.W, padx=16, pady=4
        )
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

    def _on_canvas_configure(self, event):
        self.canvas.itemconfig(self.canvas_window, width=event.width)

    def _select_provider(self, provider_id):
        self.current_provider = provider_id
        # 更新导航按钮选中态
        for pid, btn in self.nav_buttons.items():
            if pid == provider_id:
                btn.configure(bg=self.c_primary_light, fg=self.c_primary, font=("Microsoft YaHei UI", 10, "bold"))
            else:
                btn.configure(bg="#FFFFFF", fg=self.c_text_main, font=("Microsoft YaHei UI", 10))

        p_info = PROVIDERS_INFO.get(provider_id, {})
        disp_name = p_info.get("displayName", provider_id)
        self.right_title_lbl.config(text=f"{disp_name} 账号管理")

        # 只有腾讯 CodeBuddy 支持每日领积分按钮
        if provider_id == "buddy":
            self.claim_btn.pack(side=tk.LEFT, padx=4)
        else:
            self.claim_btn.pack_forget()

        self._refresh_account_cards()

    def _refresh_account_cards(self):
        # 清空当前卡片
        for widget in self.card_list_frame.winfo_children():
            widget.destroy()

        accounts = self.mgr.list_accounts(self.current_provider)
        if not accounts:
            empty_box = tk.Frame(self.card_list_frame, bg="#FFFFFF", padx=24, pady=32, bd=1, relief=tk.SOLID)
            empty_box.pack(fill=tk.X, pady=12)
            tk.Label(
                empty_box, text=f"暂无已配置的 {self.current_provider} 账号",
                font=("Microsoft YaHei UI", 11), fg=self.c_text_sub, bg="#FFFFFF"
            ).pack()
            tk.Label(
                empty_box, text="点击右上角「+ 新建账号」添加凭据，或在 DSH Desktop 中完成登录即可自动同步",
                font=("Microsoft YaHei UI", 9), fg="#9CA3AF", bg="#FFFFFF"
            ).pack(pady=(6, 0))
            return

        for acc in accounts:
            self._render_account_card(acc)

    def _render_account_card(self, acc):
        card = tk.Frame(self.card_list_frame, bg="#FFFFFF", bd=1, relief=tk.SOLID, padx=18, pady=14)
        card.pack(fill=tk.X, pady=8)

        # 顶部：状态圆点 + 昵称 + 启用徽章
        header_row = tk.Frame(card, bg="#FFFFFF")
        header_row.pack(fill=tk.X)

        is_enabled = acc.get("enabled", True)
        dot_color = self.c_green if is_enabled else "#9CA3AF"
        dot_lbl = tk.Label(header_row, text="●", fg=dot_color, font=("Segoe UI", 12), bg="#FFFFFF")
        dot_lbl.pack(side=tk.LEFT, padx=(0, 6))

        nickname = acc.get("nickname", "未命名账号")
        name_lbl = tk.Label(header_row, text=nickname, font=("Microsoft YaHei UI", 11, "bold"), fg=self.c_text_main, bg="#FFFFFF")
        name_lbl.pack(side=tk.LEFT)

        badge_text = "已启用" if is_enabled else "已停用"
        badge_bg = "#ECFDF5" if is_enabled else "#F3F4F6"
        badge_fg = self.c_green if is_enabled else self.c_text_sub
        badge_lbl = tk.Label(
            header_row, text=badge_text, font=("Microsoft YaHei UI", 8, "bold"),
            bg=badge_bg, fg=badge_fg, padx=8, pady=2
        )
        badge_lbl.pack(side=tk.RIGHT)

        # 信息行：凭据标识与有效期
        info_frame = tk.Frame(card, bg="#FFFFFF")
        info_frame.pack(fill=tk.X, pady=(10, 12))

        ref = acc.get("credentialRef", "未知标识")
        ref_row = tk.Frame(info_frame, bg="#FFFFFF")
        ref_row.pack(fill=tk.X, pady=1)
        tk.Label(ref_row, text="凭据标识", font=("Microsoft YaHei UI", 9), fg=self.c_text_sub, bg="#FFFFFF", width=10, anchor=tk.W).pack(side=tk.LEFT)
        tk.Label(ref_row, text=ref, font=("Consolas", 9), fg=self.c_text_main, bg="#FFFFFF").pack(side=tk.LEFT)

        expires_at = acc.get("expiresAt", 0)
        exp_str = "本地服务 · 随IDE常驻" if acc.get("provider") == "antigravity" else "长期有效 · 自动续期"
        if expires_at and expires_at > 0:
            dt = datetime.fromtimestamp(expires_at / 1000.0)
            exp_str = f"{dt.strftime('%m月%d日 %H:%M')} · 自动续期"

        exp_row = tk.Frame(info_frame, bg="#FFFFFF")
        exp_row.pack(fill=tk.X, pady=1)
        tk.Label(exp_row, text="有效期", font=("Microsoft YaHei UI", 9), fg=self.c_text_sub, bg="#FFFFFF", width=10, anchor=tk.W).pack(side=tk.LEFT)
        tk.Label(exp_row, text=exp_str, font=("Microsoft YaHei UI", 9), fg=self.c_text_main, bg="#FFFFFF").pack(side=tk.LEFT)

        # 测速/状态动态提示
        test_info_lbl = tk.Label(info_frame, text="", font=("Microsoft YaHei UI", 8), fg=self.c_text_sub, bg="#FFFFFF")
        test_info_lbl.pack(fill=tk.X, pady=(2, 0))

        # 分割细线
        tk.Frame(card, height=1, bg="#F3F4F6").pack(fill=tk.X, pady=(2, 10))

        # 底部操作按钮
        action_row = tk.Frame(card, bg="#FFFFFF")
        action_row.pack(fill=tk.X)

        acc_id = acc.get("id")

        retest_btn = tk.Button(
            action_row, text="重测", font=("Microsoft YaHei UI", 9),
            bg="#FFFFFF", fg=self.c_text_main, relief=tk.FLAT, bd=1,
            highlightbackground=self.c_border, highlightthickness=1, padx=12, pady=3,
            cursor="hand2", command=lambda a=acc, lbl=test_info_lbl: self._on_retest_single(a, lbl)
        )
        retest_btn.pack(side=tk.RIGHT, padx=4)

        reset_btn = tk.Button(
            action_row, text="重置", font=("Microsoft YaHei UI", 9),
            bg="#FFFFFF", fg=self.c_text_main, relief=tk.FLAT, bd=1,
            highlightbackground=self.c_border, highlightthickness=1, padx=12, pady=3,
            cursor="hand2", command=lambda a=acc, lbl=test_info_lbl: self._on_reset_single(a, lbl)
        )
        reset_btn.pack(side=tk.RIGHT, padx=4)

        toggle_text = "停用" if is_enabled else "启用"
        toggle_btn = tk.Button(
            action_row, text=toggle_text, font=("Microsoft YaHei UI", 9),
            bg="#FFFFFF", fg=self.c_text_main, relief=tk.FLAT, bd=1,
            highlightbackground=self.c_border, highlightthickness=1, padx=12, pady=3,
            cursor="hand2", command=lambda aid=acc_id: self._on_toggle_account(aid)
        )
        toggle_btn.pack(side=tk.RIGHT, padx=4)

        # 内置 Antigravity 不提供删除
        if acc.get("source") != "builtin":
            del_btn = tk.Button(
                action_row, text="删除", font=("Microsoft YaHei UI", 9),
                bg="#FFFFFF", fg=self.c_red, relief=tk.FLAT, bd=1,
                highlightbackground="#FCA5A5", highlightthickness=1, padx=12, pady=3,
                cursor="hand2", command=lambda aid=acc_id: self._on_delete_account(aid)
            )
            del_btn.pack(side=tk.RIGHT, padx=4)

    def _on_retest_single(self, acc, status_lbl):
        status_lbl.config(text="正在测速中...", fg=self.c_text_sub)

        def _worker():
            if acc.get("provider") == "antigravity":
                inst = self.mgr.antigravity_client.discover(force_refresh=True)
                if inst:
                    msg = f"已直连本地 Language Server (PID {inst.get('pid')}, 端口 {inst.get('port')})"
                    color = self.c_green
                else:
                    msg = "未发现本地 Antigravity 运行实例"
                    color = self.c_red
            else:
                ok, detail = BuddyClient.test_connection(acc)
                msg = f"测速结果: {detail}"
                color = self.c_green if ok else self.c_red

            self.after(0, lambda: status_lbl.config(text=msg, fg=color))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_reset_single(self, acc, status_lbl):
        acc["_cooldown_until"] = 0
        status_lbl.config(text="已解除限流冷却状态", fg=self.c_green)

    def _on_toggle_account(self, acc_id):
        self.mgr.toggle_account(acc_id)
        self._refresh_account_cards()
        if self.on_update_callback:
            self.on_update_callback()

    def _on_delete_account(self, acc_id):
        acc = self.mgr.get_account_by_id(acc_id)
        name = acc.get("nickname", "该账号") if acc else "该账号"
        if messagebox.askyesno("确认删除", f"确定从账号池中删除 [{name}] 吗？"):
            self.mgr.delete_account(acc_id)
            self._refresh_account_cards()
            if self.on_update_callback:
                self.on_update_callback()

    def _on_claim_all(self):
        """一键领取当前 Provider 下的所有账号积分"""
        accounts = self.mgr.list_accounts(self.current_provider)
        valid_accs = [a for a in accounts if a.get("enabled", True)]
        if not valid_accs:
            messagebox.showinfo("提示", "当前没有已启用的账号可领取积分。")
            return

        self.status_bar.config(text="正在一键领取积分中，请稍候...")

        def _worker():
            results = []
            for acc in valid_accs:
                name = acc.get("nickname", "账号")
                ok, msg = BuddyClient.claim_credits(acc)
                results.append(f"[{name}]: {msg}")
                time.sleep(0.5)

            res_text = "\n".join(results)
            self.after(0, lambda: messagebox.showinfo("一键领取结果", res_text))
            self.after(0, lambda: self.status_bar.config(text="一键领取操作完成"))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_retest_all(self):
        self.status_bar.config(text="正在批量重测所有账号延迟与健康度...")
        self._refresh_account_cards()
        self.after(500, lambda: self.status_bar.config(text="就绪"))

    def _on_reset_all(self):
        for acc in self.mgr.accounts:
            acc["_cooldown_until"] = 0
        messagebox.showinfo("提示", "已重置所有账号的频控冷却标记！")
        self._refresh_account_cards()

    def _on_add_account(self):
        dialog = tk.Toplevel(self)
        dialog.title(f"新建 {self.current_provider} 账号")
        dialog.geometry("460x280")
        dialog.configure(bg="#FFFFFF")
        dialog.transient(self)
        dialog.grab_set()

        tk.Label(dialog, text=f"添加 {self.current_provider} 凭据", font=("Microsoft YaHei UI", 12, "bold"), bg="#FFFFFF").pack(anchor=tk.W, padx=20, pady=(16, 8))

        form_frame = tk.Frame(dialog, bg="#FFFFFF", padx=20)
        form_frame.pack(fill=tk.BOTH, expand=True)

        tk.Label(form_frame, text="账号昵称:", font=("Microsoft YaHei UI", 9), bg="#FFFFFF").grid(row=0, column=0, sticky=tk.W, pady=6)
        name_entry = tk.Entry(form_frame, font=("Microsoft YaHei UI", 9), width=32)
        name_entry.grid(row=0, column=1, sticky=tk.W, pady=6)
        name_entry.insert(0, f"我的{self.current_provider}账号")

        tk.Label(form_frame, text="Access Token:", font=("Microsoft YaHei UI", 9), bg="#FFFFFF").grid(row=1, column=0, sticky=tk.W, pady=6)
        token_entry = tk.Entry(form_frame, font=("Microsoft YaHei UI", 9), width=32, show="*")
        token_entry.grid(row=1, column=1, sticky=tk.W, pady=6)

        btn_row = tk.Frame(dialog, bg="#FFFFFF", padx=20, pady=16)
        btn_row.pack(fill=tk.X)

        def _confirm():
            name = name_entry.get().strip()
            token = token_entry.get().strip()
            if not token:
                messagebox.showerror("错误", "Access Token 不能为空！", parent=dialog)
                return
            self.mgr.add_custom_account(self.current_provider, name or "新账号", token)
            dialog.destroy()
            self._refresh_account_cards()
            if self.on_update_callback:
                self.on_update_callback()

        tk.Button(btn_row, text="取消", font=("Microsoft YaHei UI", 9), bg="#F3F4F6", relief=tk.FLAT, padx=12, pady=4, command=dialog.destroy).pack(side=tk.RIGHT, padx=6)
        tk.Button(btn_row, text="保存账号", font=("Microsoft YaHei UI", 9, "bold"), bg=self.c_primary, fg="#FFFFFF", relief=tk.FLAT, padx=14, pady=4, command=_confirm).pack(side=tk.RIGHT)

    def _open_config_file(self):
        settings_path, _ = get_dsh_paths()
        target = settings_path if os.path.exists(settings_path) else LOCAL_STORE_PATH
        try:
            if sys.platform == "win32":
                os.startfile(os.path.dirname(target))
            else:
                subprocess.Popen(["explorer", os.path.dirname(target)])
        except Exception as e:
            messagebox.showinfo("文件路径", f"配置文件所在路径:\n{target}")
