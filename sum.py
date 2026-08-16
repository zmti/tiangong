def sum_two_numbers(a, b):
    """计算两个数的和"""
    return a + b

if __name__ == "__main__":
    # 使用传入的参数，默认值为 3 和 4
    import sys
    if len(sys.argv) >= 3:
        a = int(sys.argv[1])
        b = int(sys.argv[2])
    else:
        a, b = 3, 4
    result = sum_two_numbers(a, b)
    print(f"{a} + {b} = {result}")
