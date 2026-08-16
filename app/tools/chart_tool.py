"""图表生成工具:根据数据生成可视化图表(柱状图、折线图、饼图等)。"""
from __future__ import annotations

import base64
import io
import json
import os
import tempfile
from typing import Optional

from app.tools.registry import tool


def _generate_chart(
    chart_type: str,
    data: dict,
    title: str = "",
    x_label: str = "",
    y_label: str = "",
    width: int = 10,
    height: int = 6,
    figsize: tuple = (10, 6),
    save_path: str = "",
) -> dict:
    """生成图表的核心函数。"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.font_manager as fm

        # 尝试使用中文字体
        font_paths = [
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "C:/Windows/Fonts/simsun.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        ]
        for fp in font_paths:
            if os.path.exists(fp):
                prop = fm.FontProperties(fname=fp)
                plt.rcParams["font.family"] = prop.get_name()
                break
        else:
            plt.rcParams["font.sans-serif"] = ["SimHei", "DejaVu Sans", "Arial"]
        plt.rcParams["axes.unicode_minus"] = False

        fig, ax = plt.subplots(figsize=figsize)

        labels = data.get("labels", [])
        datasets = data.get("datasets", [])

        if chart_type == "bar":
            if len(datasets) == 1:
                ax.bar(labels, datasets[0].get("values", []), color=datasets[0].get("color", "#4CAF50"))
            else:
                import numpy as np
                x = np.arange(len(labels))
                width_bar = 0.8 / len(datasets)
                for i, ds in enumerate(datasets):
                    ax.bar(x + i * width_bar, ds.get("values", []), width_bar, label=ds.get("name", f"Dataset {i+1}"))
                ax.set_xticks(x + 0.4)
                ax.set_xticklabels(labels)
                ax.legend()

        elif chart_type == "line":
            for ds in datasets:
                ax.plot(labels, ds.get("values", []), marker="o", label=ds.get("name", ""), linewidth=2)
            if len(datasets) > 1:
                ax.legend()

        elif chart_type == "pie":
            values = datasets[0].get("values", []) if datasets else []
            colors = datasets[0].get("colors", None) if datasets else None
            explode = [0.05] * len(labels) if len(labels) <= 10 else None
            ax.pie(values, labels=labels, autopct="%1.1f%%", startangle=90,
                   colors=colors, explode=explode, textprops={"fontsize": 10})

        elif chart_type == "scatter":
            for ds in datasets:
                x_data = ds.get("x", [])
                y_data = ds.get("y", [])
                ax.scatter(x_data, y_data, label=ds.get("name", ""), s=50, alpha=0.7)
            if len(datasets) > 1:
                ax.legend()

        elif chart_type == "area":
            for ds in datasets:
                ax.fill_between(labels, ds.get("values", []), alpha=0.3, label=ds.get("name", ""))
                ax.plot(labels, ds.get("values", []), linewidth=1)
            if len(datasets) > 1:
                ax.legend()

        elif chart_type == "horizontal_bar":
            if len(datasets) == 1:
                ax.barh(labels, datasets[0].get("values", []), color=datasets[0].get("color", "#2196F3"))
            else:
                import numpy as np
                y = np.arange(len(labels))
                height_bar = 0.8 / len(datasets)
                for i, ds in enumerate(datasets):
                    ax.barh(y + i * height_bar, ds.get("values", []), height_bar, label=ds.get("name", ""))
                ax.set_yticks(y + 0.4)
                ax.set_yticklabels(labels)
                ax.legend()

        elif chart_type == "stacked_bar":
            import numpy as np
            x = np.arange(len(labels))
            bottom = np.zeros(len(labels))
            for ds in datasets:
                vals = ds.get("values", [])
                ax.bar(x, vals, bottom=bottom, label=ds.get("name", ""))
                bottom += np.array(vals)
            ax.set_xticks(x)
            ax.set_xticklabels(labels)
            ax.legend()

        else:
            plt.close(fig)
            return {"error": f"不支持的图表类型: {chart_type}"}

        if title:
            ax.set_title(title, fontsize=14, fontweight="bold")
        if x_label and chart_type not in ("pie",):
            ax.set_xlabel(x_label)
        if y_label and chart_type not in ("pie",):
            ax.set_ylabel(y_label)

        plt.tight_layout()

        if save_path:
            fig.savefig(save_path, dpi=150, bbox_inches="tight")
            plt.close(fig)
            return {"status": "ok", "path": save_path, "format": "png"}

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)

        # 落盘本地静态目录并返回 local_url(与图片生成工具一致,前端直接展示、历史可回放)
        try:
            from app.tools.image_tool import _image_url, _save_image
            name = _save_image(buf.read(), "png")
            return {"status": "ok", "format": "local_url", "size": "chart", "image_url": _image_url(name)}
        except Exception:
            buf.seek(0)
            img_b64 = base64.b64encode(buf.read()).decode()
            return {"status": "ok", "image_base64": img_b64, "format": "png"}

    except ImportError:
        return {"error": "需要安装 matplotlib: pip install matplotlib"}
    except Exception as e:
        return {"error": f"生成图表失败: {e}"}


@tool("generate_chart", "根据数据生成可视化图表", timeout=30)
def generate_chart(
    chart_type: str,
    data: str,
    title: str = "",
    x_label: str = "",
    y_label: str = "",
) -> dict:
    """
    生成可视化图表。

    Args:
        chart_type: 图表类型(bar/line/pie/scatter/area/horizontal_bar/stacked_bar)
        data: JSON 格式数据,格式: {"labels": [...], "datasets": [{"name": "...", "values": [...]}]}
        title: 图表标题
        x_label: X 轴标签
        y_label: Y 轴标签

    Returns:
        图表数据(base64 图片)
    """
    try:
        chart_data = json.loads(data) if isinstance(data, str) else data
    except json.JSONDecodeError:
        return {"error": "data 参数必须是有效的 JSON"}

    return _generate_chart(
        chart_type=chart_type,
        data=chart_data,
        title=title,
        x_label=x_label,
        y_label=y_label,
    )


@tool("generate_bar_chart", "生成柱状图", timeout=20)
def generate_bar_chart(
    labels: str,
    values: str,
    title: str = "",
    y_label: str = "",
    color: str = "#4CAF50",
) -> dict:
    """
    快捷生成柱状图。

    Args:
        labels: JSON 数组,分类标签,如 ["A", "B", "C"]
        values: JSON 数组,数值,如 [10, 20, 30]
        title: 图表标题
        y_label: Y 轴标签
        color: 柱子颜色

    Returns:
        图表数据
    """
    try:
        label_list = json.loads(labels) if isinstance(labels, str) else labels
        value_list = json.loads(values) if isinstance(values, str) else values
    except json.JSONDecodeError:
        return {"error": "labels 和 values 必须是有效的 JSON 数组"}

    data = {"labels": label_list, "datasets": [{"name": "Data", "values": value_list, "color": color}]}
    return _generate_chart("bar", data, title=title, y_label=y_label)


@tool("generate_line_chart", "生成折线图", timeout=20)
def generate_line_chart(
    labels: str,
    values: str,
    title: str = "",
    y_label: str = "",
    series_name: str = "",
) -> dict:
    """
    快捷生成折线图。

    Args:
        labels: JSON 数组,X 轴标签
        values: JSON 数组,Y 轴数值
        title: 图表标题
        y_label: Y 轴标签
        series_name: 数据系列名称

    Returns:
        图表数据
    """
    try:
        label_list = json.loads(labels) if isinstance(labels, str) else labels
        value_list = json.loads(values) if isinstance(values, str) else values
    except json.JSONDecodeError:
        return {"error": "labels 和 values 必须是有效的 JSON 数组"}

    data = {"labels": label_list, "datasets": [{"name": series_name or "Data", "values": value_list}]}
    return _generate_chart("line", data, title=title, y_label=y_label)


@tool("generate_pie_chart", "生成饼图", timeout=20)
def generate_pie_chart(
    labels: str,
    values: str,
    title: str = "",
) -> dict:
    """
    快捷生成饼图。

    Args:
        labels: JSON 数组,各部分标签
        values: JSON 数组,各部分数值
        title: 图表标题

    Returns:
        图表数据
    """
    try:
        label_list = json.loads(labels) if isinstance(labels, str) else labels
        value_list = json.loads(values) if isinstance(values, str) else values
    except json.JSONDecodeError:
        return {"error": "labels 和 values 必须是有效的 JSON 数组"}

    data = {"labels": label_list, "datasets": [{"values": value_list}]}
    return _generate_chart("pie", data, title=title)


@tool("generate_scatter_chart", "生成散点图", timeout=20)
def generate_scatter_chart(
    x_data: str,
    y_data: str,
    title: str = "",
    x_label: str = "",
    y_label: str = "",
    series_name: str = "",
) -> dict:
    """
    快捷生成散点图。

    Args:
        x_data: JSON 数组,X 轴数据
        y_data: JSON 数组,Y 轴数据
        title: 图表标题
        x_label: X 轴标签
        y_label: Y 轴标签
        series_name: 数据系列名称

    Returns:
        图表数据
    """
    try:
        x_list = json.loads(x_data) if isinstance(x_data, str) else x_data
        y_list = json.loads(y_data) if isinstance(y_data, str) else y_data
    except json.JSONDecodeError:
        return {"error": "x_data 和 y_data 必须是有效的 JSON 数组"}

    data = {"labels": [], "datasets": [{"name": series_name or "Data", "x": x_list, "y": y_list}]}
    return _generate_chart("scatter", data, title=title, x_label=x_label, y_label=y_label)


@tool("generate_multi_line_chart", "生成多系列折线图", timeout=20)
def generate_multi_line_chart(
    labels: str,
    datasets: str,
    title: str = "",
    x_label: str = "",
    y_label: str = "",
) -> dict:
    """
    生成多系列折线图。

    Args:
        labels: JSON 数组,X 轴标签
        datasets: JSON 数组,格式: [{"name": "系列1", "values": [1,2,3]}, {"name": "系列2", "values": [4,5,6]}]
        title: 图表标题
        x_label: X 轴标签
        y_label: Y 轴标签

    Returns:
        图表数据
    """
    try:
        label_list = json.loads(labels) if isinstance(labels, str) else labels
        ds_list = json.loads(datasets) if isinstance(datasets, str) else datasets
    except json.JSONDecodeError:
        return {"error": "labels 和 datasets 必须是有效的 JSON"}

    data = {"labels": label_list, "datasets": ds_list}
    return _generate_chart("line", data, title=title, x_label=x_label, y_label=y_label)


@tool("save_chart_to_file", "将图表保存为文件", timeout=30)
def save_chart_to_file(
    chart_type: str,
    data: str,
    file_path: str,
    title: str = "",
) -> dict:
    """
    生成图表并保存为文件。

    Args:
        chart_type: 图表类型
        data: JSON 格式数据
        file_path: 保存路径(如 output.png)
        title: 图表标题

    Returns:
        保存结果
    """
    try:
        chart_data = json.loads(data) if isinstance(data, str) else data
    except json.JSONDecodeError:
        return {"error": "data 参数必须是有效的 JSON"}

    os.makedirs(os.path.dirname(file_path) if os.path.dirname(file_path) else ".", exist_ok=True)
    result = _generate_chart(chart_type=chart_type, data=chart_data, title=title, save_path=file_path)
    return result
