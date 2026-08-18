"""AST 符号提取增强版测试(替代纯正则方案)。"""
import pytest

from app.core.ast_symbols import extract_symbols


def test_python_functions_and_classes():
    src = '''\
def hello(name: str) -> str:
    return f"Hello {name}"

async def fetch_data(url: str) -> dict:
    data = await call(url)
    return data

class UserService:
    def __init__(self, db):
        self.db = db

    @classmethod
    def create(cls):
        return cls(None)
'''
    syms = extract_symbols(src, ".py", "test.py")
    names = {s["name"] for s in syms}
    assert "hello" in names
    assert "fetch_data" in names
    assert "UserService" in names
    kinds = {s["name"]: s["kind"] for s in syms}
    assert kinds["hello"] == "function"
    assert kinds["fetch_data"] == "function"
    assert kinds["UserService"] == "class"


def test_typescript_interfaces_and_types():
    src = '''\
interface User {
    id: string;
    name: string;
}

type Result<T> = {
    data: T;
    error: string | null;
};

class ApiService {
    async fetch(id: string): Promise<Result<string>> {
        return { data: id, error: null };
    }
}
'''
    syms = extract_symbols(src, ".ts", "test.ts")
    names = {s["name"] for s in syms}
    assert "User" in names
    assert "ApiService" in names
    kinds = {s["name"]: s["kind"] for s in syms}
    assert kinds.get("User") == "interface"


def test_java_methods_and_classes():
    src = '''\
public class Calculator {
    public int add(int a, int b) {
        return a + b;
    }

    private static String format(double v) {
        return String.valueOf(v);
    }
}
'''
    syms = extract_symbols(src, ".java", "Calc.java")
    names = {s["name"] for s in syms}
    assert "Calculator" in names
    assert "add" in names
    assert "format" in names


def test_go_functions_and_types():
    src = '''\
package main

import "fmt"

func main() {
    fmt.Println("hello")
}

type Config struct {
    Host string
}
'''
    syms = extract_symbols(src, ".go", "main.go")
    names = {s["name"] for s in syms}
    assert "main" in names
    assert "Config" in names
    kinds = {s["name"]: s["kind"] for s in syms}
    assert kinds["main"] == "function"
    assert kinds["Config"] == "type"


def test_rust_functions_and_structs():
    src = '''\
fn main() {
    let x = 42;
    println!("{}", x);
}

pub struct Point {
    pub x: f64,
    pub y: f64,
}

impl Point {
    pub fn new(x: f64, y: f64) -> Self {
        Point { x, y }
    }
}
'''
    syms = extract_symbols(src, ".rs", "main.rs")
    names = {s["name"] for s in syms}
    assert "main" in names
    assert "Point" in names
    assert "new" in names


def test_js_arrow_functions():
    src = '''\
const handleClick = (e) => {
    console.log(e);
};

export default function App() {
    return <div />;
}
'''
    syms = extract_symbols(src, ".jsx", "App.jsx")
    names = {s["name"] for s in syms}
    assert "handleClick" in names
    assert "App" in names


def test_symbols_skip_comments():
    src = '''\
// def fake_function():
# class FakeClass:
def real_function():
    pass
'''
    syms = extract_symbols(src, ".py", "test.py")
    names = {s["name"] for s in syms}
    assert "fake_function" not in names
    assert "FakeClass" not in names
    assert "real_function" in names


def test_max_symbols_limit():
    src = "\n".join(f"def func{i}(): pass" for i in range(20))
    syms = extract_symbols(src, ".py", "test.py", max_symbols=5)
    assert len(syms) <= 5


def test_unsupported_extension():
    syms = extract_symbols("some content", ".unknown_ext")
    assert syms == []
