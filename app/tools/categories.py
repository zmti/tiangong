"""工具分类表:按业务域给全部工具分组。

工具路由(router)据此在每轮对话前预选相关工具子集,
避免 100+ 工具全量注入导致调用精度下降(BFCL 建议的 tiered 做法)。
新增工具时,在此登记分类;未登记的工具默认归类为 "其他"。
"""
from __future__ import annotations

# 工具名 -> 业务域
TOOL_CATEGORIES: dict[str, str] = {
    # ---- 基础 ----
    "get_current_time": "基础",
    "calculate": "基础",
    "days_between": "基础",
    # ---- 代码执行 ----
    "run_code": "代码",
    "run_terminal": "代码",
    "code_search": "代码",
    "code_symbols": "代码",
    "project_tree": "代码",
    "code_context": "代码",
    "code_semantic_search": "代码",
    # ---- Git ----
    "git_status": "代码库",
    "git_log": "代码库",
    "git_diff": "代码库",
    "git_add": "代码库",
    "git_commit": "代码库",
    "git_branch": "代码库",
    "git_checkout": "代码库",
    # ---- 文件 ----
    "search_files": "文件",
    "list_directory": "文件",
    "read_document": "文件",
    "write_file": "文件",
    "edit_file": "文件",
    "file_history": "文件",
    "file_restore": "文件",
    "file_undo": "文件",
    # ---- 网络 ----
    "fetch_webpage": "网络",
    "extract_text": "网络",
    # ---- 浏览器 ----
    "browser_open": "浏览器",
    "browser_screenshot": "浏览器",
    "browser_get_text": "浏览器",
    "browser_click": "浏览器",
    "browser_type": "浏览器",
    "browser_scroll": "浏览器",
    "browser_back": "浏览器",
    "browser_forward": "浏览器",
    "browser_get_links": "浏览器",
    "browser_fill_form": "浏览器",
    "browser_evaluate": "浏览器",
    "browser_wait": "浏览器",
    "browser_close": "浏览器",
    # ---- 图像 / 视频 ----
    "generate_image": "图像生成",
    "generate_video": "图像生成",
    # ---- 图表 ----
    "generate_chart": "图表",
    "generate_bar_chart": "图表",
    "generate_line_chart": "图表",
    "generate_pie_chart": "图表",
    "generate_scatter_chart": "图表",
    "generate_multi_line_chart": "图表",
    "save_chart_to_file": "图表",
    # ---- 视觉 ----
    "analyze_image": "视觉",
    "extract_text_from_image": "视觉",
    "compare_images": "视觉",
    "describe_chart": "视觉",
    # ---- 邮件 ----
    "send_email": "邮件",
    "read_emails": "邮件",
    "search_emails": "邮件",
    "get_email_count": "邮件",
    "mark_email_read": "邮件",
    "delete_email": "邮件",
    "list_email_folders": "邮件",
    # ---- 笔记 ----
    "create_note": "笔记",
    "update_note": "笔记",
    "delete_note": "笔记",
    "get_note": "笔记",
    "list_notes": "笔记",
    "search_notes": "笔记",
    "pin_note": "笔记",
    "list_note_categories": "笔记",
    "list_note_tags": "笔记",
    "get_note_stats": "笔记",
    "export_notes": "笔记",
    # ---- 待办 ----
    "create_todo": "待办",
    "list_todos": "待办",
    "toggle_todo": "待办",
    "delete_todo": "待办",
    "get_todo_stats": "待办",
    # ---- 日程 ----
    "create_schedule": "日程",
    "list_schedules": "日程",
    "get_schedule": "日程",
    "delete_schedule": "日程",
    "pause_schedule": "日程",
    "resume_schedule": "日程",
    "run_schedule_now": "日程",
    "run_custom_task": "日程",
    # ---- 数据库 ----
    "query_sqlite": "数据库",
    "list_sqlite_tables": "数据库",
    "describe_table": "数据库",
    # ---- 知识库 ----
    "rag_search": "知识库",
    # ---- 长期记忆 ----
    "memory_save": "记忆",
    "memory_search": "记忆",
    "memory_forget": "记忆",
    "memory_update": "记忆",
    "memory_list": "记忆",
    "memory_stats": "记忆",
    # ---- 任务规划 ----
    "set_plan": "规划",
    "update_plan": "规划",
    # ---- 人机协作 ----
    "request_confirmation": "协作",
    "approve_confirmation": "协作",
    "reject_confirmation": "协作",
    "list_pending_confirmations": "协作",
    "get_confirmation": "协作",
    "auto_confirm_check": "协作",
    "delegate_task": "协作",
    "list_roles": "协作",
    "handoff_to_role": "协作",
}

# 系统级工具:无论任务如何都常驻(规划与记忆基础设施),不参与路由裁剪
SYSTEM_TOOLS: tuple[str, ...] = (
    "set_plan",
    "update_plan",
    "memory_save",
    "memory_search",
    "memory_forget",
    "memory_list",
    "memory_stats",
    "delegate_task",
)


def get_category(tool_name: str) -> str:
    """返回工具所属分类,未登记时归为 '其他'。"""
    return TOOL_CATEGORIES.get(tool_name, "其他")
