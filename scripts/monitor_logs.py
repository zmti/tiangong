"""日志监控脚本 - 检查并发相关警告。

使用方法:
    python scripts/monitor_logs.py [--follow] [--hours 24]
    
选项:
    --follow   实时跟踪日志文件
    --hours    检查最近 N 小时的日志(默认 24)
"""
import os
import re
import sys
from pathlib import Path
from datetime import datetime, timedelta


# 需要监控的警告模式
WARN_PATTERNS = {
    'threading_lock_in_async': re.compile(
        r'thread.*Lock|threading.*lock|blocking.*lock',
        re.IGNORECASE
    ),
    'concurrent_access': re.compile(
        r'concurrent|race.?condition|thread.?safety',
        re.IGNORECASE
    ),
    'checkpoint_warning': re.compile(
        r'checkpoint.*fail|save.*fail',
        re.IGNORECASE
    ),
    'security_warning': re.compile(
        r'timing.?attack|constant.?time|hmac',
        re.IGNORECASE
    ),
}


def find_log_files(base_dir: str) -> list[Path]:
    """查找所有日志文件。"""
    log_patterns = ['*.log', 'logs/**/*', 'data/logs/**/*']
    log_files = []
    
    base = Path(base_dir)
    for pattern in log_patterns:
        log_files.extend(base.glob(pattern))
    
    # 也检查 data 目录下的日志
    data_dir = base / 'data'
    if data_dir.exists():
        for sub_dir in ['logs', 'traces']:
            dir_path = data_dir / sub_dir
            if dir_path.exists():
                log_files.extend(dir_path.glob('**/*.log'))
                log_files.extend(dir_path.glob('**/*.jsonl'))
    
    return sorted(set(log_files))


def analyze_log_file(filepath: Path, hours: int = 24) -> dict:
    """分析单个日志文件。"""
    results = {
        'file': str(filepath),
        'warnings': [],
        'error_count': 0,
        'line_count': 0,
    }
    
    try:
        # 计算时间阈值
        cutoff_time = datetime.now() - timedelta(hours=hours)
        
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            for line_num, line in enumerate(f, 1):
                results['line_count'] += 1
                
                # 检查时间戳
                log_time = None
                ts_match = re.search(r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})', line)
                if ts_match:
                    try:
                        log_time = datetime.fromisoformat(ts_match.group(1).replace('T', ' '))
                    except ValueError:
                        pass
                
                # 如果有关联的时间戳且早于阈值，跳过
                if log_time and log_time < cutoff_time:
                    continue
                
                # 检查警告模式
                for pattern_name, pattern in WARN_PATTERNS.items():
                    if pattern.search(line):
                        results['warnings'].append({
                            'line_num': line_num,
                            'pattern': pattern_name,
                            'content': line.strip()[:200]
                        })
                
                # 统计错误
                if 'ERROR' in line.upper() or ' Traceback' in line:
                    results['error_count'] += 1
                    
    except Exception as e:
        results['error'] = str(e)
    
    return results


def generate_report(results: list[dict]) -> str:
    """生成分析报告。"""
    lines = [
        "=" * 60,
        "P0 修复 - 日志监控报告",
        "=" * 60,
        f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    
    total_warnings = 0
    total_errors = 0
    
    for result in results:
        lines.append(f"文件: {result['file']}")
        lines.append(f"  分析行数: {result['line_count']}")
        lines.append(f"  错误数量: {result['error_count']}")
        
        if result['warnings']:
            total_warnings += len(result['warnings'])
            lines.append(f"  警告数量: {len(result['warnings'])}")
            for w in result['warnings'][:5]:  # 只显示前5个
                lines.append(f"    - [{w['pattern']}] L{w['line_num']}: {w['content'][:100]}...")
            if len(result['warnings']) > 5:
                lines.append(f"    ... 还有 {len(result['warnings']) - 5} 个警告")
        else:
            lines.append("  警告数量: 0 ✓")
        
        lines.append("")
        
        total_errors += result['error_count']
    
    lines.append("=" * 60)
    lines.append("总结")
    lines.append("=" * 60)
    lines.append(f"总警告数: {total_warnings}")
    lines.append(f"总错误数: {total_errors}")
    
    if total_warnings == 0 and total_errors == 0:
        lines.append("\n✓ 未发现并发相关警告，P0 修复生效")
    else:
        lines.append(f"\n⚠ 发现 {total_warnings} 个警告，请检查")
    
    return "\n".join(lines)


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='日志监控脚本')
    parser.add_argument('--follow', action='store_true', help='实时跟踪')
    parser.add_argument('--hours', type=int, default=24, help='检查最近 N 小时')
    parser.add_argument('--log-dir', help='日志目录(默认自动检测)')
    args = parser.parse_args()
    
    # 确定日志目录
    if args.log_dir:
        base_dir = args.log_dir
    else:
        # 尝试常见位置
        candidates = [
            Path(__file__).parent.parent / 'data' / 'logs',
            Path(__file__).parent.parent / 'logs',
            Path(__file__).parent.parent,
        ]
        base_dir = None
        for c in candidates:
            if c.exists():
                base_dir = str(c)
                break
        if not base_dir:
            base_dir = str(Path(__file__).parent.parent)
    
    print(f"扫描目录: {base_dir}")
    print(f"时间范围: 最近 {args.hours} 小时\n")
    
    # 查找日志文件
    log_files = find_log_files(base_dir)
    if not log_files:
        print("未找到日志文件")
        return 0
    
    print(f"找到 {len(log_files)} 个日志文件\n")
    
    # 分析每个文件
    results = []
    for lf in log_files:
        print(f"分析: {lf.name}...", end=" ")
        result = analyze_log_file(lf, args.hours)
        results.append(result)
        status = "✓" if not result['warnings'] and result['error_count'] == 0 else "⚠"
        print(status)
    
    # 生成报告
    print("\n" + generate_report(results))
    
    # 如果有警告，返回非零退出码
    total_warnings = sum(len(r['warnings']) for r in results)
    return 1 if total_warnings > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
