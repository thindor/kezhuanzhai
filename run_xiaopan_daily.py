"""小盘债每日自动刷新入口（供 Windows 计划任务 / WorkBuddy 自动化调用）。

只做一件事：对全市场小盘债候选重算实时价、到期赎回价、历史最高，
写回 bonds 表（current_price / redemption_price / mini_hist_max / mini_hist_updated_at）。
等价于页面「刷新」按钮，但无人值守、每日收盘后跑一次。
"""
import mini_bond


def main():
    mini_bond.ensure_columns()
    now = mini_bond.refresh_all()
    print("xiaopan refreshed at", now or "n/a")


if __name__ == "__main__":
    main()
