"""TTS 诊断脚本 - 帮助排查朗读失败问题。"""
import asyncio
import sys
from pathlib import Path


async def diagnose_tts():
    """诊断 TTS 配置和问题。"""
    print("\n=== TTS 诊断 ===\n")
    
    # 1. 检查 edge-tts 是否安装
    print("1. 检查 edge-tts 库...")
    try:
        import edge_tts
        print(f"   ✓ edge-tts 已安装: {edge_tts.__version__ if hasattr(edge_tts, '__version__') else 'unknown'}")
    except ImportError as e:
        print(f"   ✗ edge-tts 未安装: {e}")
        print("   解决方案: pip install edge-tts")
        return False
    
    # 2. 检查配置
    print("\n2. 检查 TTS 配置...")
    try:
        from app.config import settings
        tts_base = settings.tts_base_url
        tts_key = settings.tts_api_key
        tts_model = settings.tts_model
        tts_voice = settings.tts_voice
        
        print(f"   TTS_BASE_URL: {'已配置' if tts_base else '未配置 (使用 edge-tts)'}")
        print(f"   TTS_API_KEY: {'已配置' if tts_key else '未配置'}")
        print(f"   TTS_MODEL: {tts_model}")
        print(f"   TTS_VOICE: {tts_voice}")
        
        if not tts_base and not tts_key:
            print("   ✓ 将使用 edge-tts (Microsoft 免费服务)")
        elif tts_base and tts_key:
            print("   ✓ 将使用 OpenAI 兼容 TTS 服务")
        else:
            print("   ⚠ 配置不完整，请检查 .env 文件")
            
    except Exception as e:
        print(f"   ✗ 配置加载失败: {e}")
        return False
    
    # 3. 测试 edge-tts
    print("\n3. 测试 edge-tts 连接...")
    try:
        test_text = "你好，这是一段测试文本。"
        communicate = edge_tts.Communicate(test_text, "zh-CN-XiaoxiaoNeural")
        
        # 尝试获取音频
        audio_chunks = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_chunks.append(chunk["data"])
        
        if audio_chunks:
            audio_data = b"".join(audio_chunks)
            print(f"   ✓ edge-tts 工作正常 (生成 {len(audio_data)} 字节)")
        else:
            print("   ✗ edge-tts 返回空音频")
            return False
            
    except Exception as e:
        print(f"   ✗ edge-tts 测试失败: {e}")
        print("   可能原因:")
        print("   - 网络连接问题")
        print("   - DNS 解析失败")
        print("   - Microsoft Edge TTS 服务暂时不可用")
        return False
    
    # 4. 测试 OpenAI 兼容 TTS (如果配置了)
    print("\n4. 测试 OpenAI 兼容 TTS...")
    try:
        from app.config import settings
        if settings.tts_base_url and settings.tts_api_key:
            import httpx
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{settings.tts_base_url.rstrip('/')}/audio/speech",
                    json={
                        "model": settings.tts_model or "tts-1",
                        "input": "测试文本",
                        "voice": settings.tts_voice or "alloy",
                    },
                    headers={"Authorization": f"Bearer {settings.tts_api_key}"},
                )
                if resp.status_code == 200:
                    print(f"   ✓ OpenAI 兼容 TTS 工作正常 ({len(resp.content)} 字节)")
                else:
                    print(f"   ✗ OpenAI 兼容 TTS 失败: HTTP {resp.status_code}")
                    print(f"      响应: {resp.text[:200]}")
        else:
            print("   ⊘ 未配置 OpenAI 兼容 TTS 服务")
    except Exception as e:
        print(f"   ✗ OpenAI 兼容 TTS 测试失败: {e}")
    
    # 5. 检查可用语音
    print("\n5. 检查可用语音...")
    try:
        from app.api.tts import VOICES
        print(f"   内置语音: {list(VOICES.keys())}")
        
        # 尝试获取 edge-tts 支持的语音
        import edge_tts
        voices = await edge_tts.list_voices()
        chinese_voices = [v for v in voices if v.language.startswith('zh')]
        print(f"   edge-tts 中文语音: {len(chinese_voices)} 个")
        for v in chinese_voices[:5]:
            print(f"     - {v.name} ({v.language})")
    except Exception as e:
        print(f"   ⚠ 无法获取语音列表: {e}")
    
    print("\n=== 诊断完成 ===\n")
    return True


async def quick_test():
    """快速测试 TTS 功能。"""
    print("\n=== 快速 TTS 测试 ===\n")
    
    try:
        import edge_tts
        import io
        
        test_cases = [
            ("你好，世界", "zh-CN-XiaoxiaoNeural"),
            ("Hello, world", "en-US-JennyNeural"),
        ]
        
        for text, voice in test_cases:
            try:
                communicate = edge_tts.Communicate(text, voice)
                buf = io.BytesIO()
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        buf.write(chunk["data"])
                data = buf.getvalue()
                print(f"✓ '{text}' -> {voice} ({len(data)} bytes)")
            except Exception as e:
                print(f"✗ '{text}' -> {voice}: {e}")
                
    except ImportError:
        print("✗ edge-tts 未安装，请运行: pip install edge-tts")
    
    print()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--quick":
        asyncio.run(quick_test())
    else:
        success = asyncio.run(diagnose_tts())
        sys.exit(0 if success else 1)
