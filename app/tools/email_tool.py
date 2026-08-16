"""邮件收发工具:发送、接收、搜索、管理邮件。"""
from __future__ import annotations

import email
import email.header
import imaplib
import json
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from pydantic import Field

from app.config import settings
from app.tools.registry import tool


class EmailConfig:
    """邮件配置。"""
    
    def __init__(
        self,
        smtp_host: str = "",
        smtp_port: int = 587,
        imap_host: str = "",
        imap_port: int = 993,
        email: str = "",
        password: str = "",
        use_ssl: bool = True,
    ):
        self.smtp_host = smtp_host or getattr(settings, "email_smtp_host", "")
        self.smtp_port = smtp_port or getattr(settings, "email_smtp_port", 587)
        self.imap_host = imap_host or getattr(settings, "email_imap_host", "")
        self.imap_port = imap_port or getattr(settings, "email_imap_port", 993)
        self.email = email or getattr(settings, "email_address", "")
        self.password = password or getattr(settings, "email_password", "")
        self.use_ssl = use_ssl
    
    @property
    def is_configured(self) -> bool:
        return bool(self.smtp_host and self.imap_host and self.email and self.password)


def _decode_header(header_value: str) -> str:
    """解码邮件头。"""
    if not header_value:
        return ""
    
    try:
        decoded_parts = email.header.decode_header(header_value)
        result = []
        for part, charset in decoded_parts:
            if isinstance(part, bytes):
                result.append(part.decode(charset or "utf-8", errors="ignore"))
            else:
                result.append(part)
        return " ".join(result)
    except Exception:
        return header_value


def _get_email_body(msg) -> str:
    """提取邮件正文。"""
    body = ""
    
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body = payload.decode(charset, errors="ignore")
                    break
            elif content_type == "text/html" and not body:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body = payload.decode(charset, errors="ignore")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="ignore")
    
    # 简单清理 HTML 标签
    if "<" in body:
        import re
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"\s+", " ", body).strip()
    
    return body[:5000] if body else ""


@tool("send_email", "发送邮件", timeout=30)
def send_email(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    html: bool = False,
) -> dict:
    """
    发送邮件。
    
    Args:
        to: 收件人(多个用逗号分隔)
        subject: 邮件主题
        body: 邮件正文
        cc: 抄送(可选,多个用逗号分隔)
        html: 是否为 HTML 格式
    
    Returns:
        发送结果
    """
    config = EmailConfig()
    
    if not config.is_configured:
        return {"error": "邮件未配置,请设置 EMAIL_SMTP_HOST/EMAIL_IMAP_HOST/EMAIL_ADDRESS/EMAIL_PASSWORD"}
    
    try:
        # 创建邮件
        msg = MIMEMultipart()
        msg["From"] = config.email
        msg["To"] = to
        msg["Subject"] = subject
        
        if cc:
            msg["Cc"] = cc
        
        # 添加正文
        content_type = "html" if html else "plain"
        msg.attach(MIMEText(body, content_type, "utf-8"))
        
        # 收件人列表
        recipients = [addr.strip() for addr in to.split(",")]
        if cc:
            recipients.extend([addr.strip() for addr in cc.split(",")])
        
        # 发送
        context = ssl.create_default_context()
        
        if config.use_ssl:
            with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, context=context) as server:
                server.login(config.email, config.password)
                server.send_message(msg, recipients=recipients)
        else:
            with smtplib.SMTP(config.smtp_host, config.smtp_port) as server:
                server.starttls(context=context)
                server.login(config.email, config.password)
                server.send_message(msg, recipients=recipients)
        
        return {
            "status": "ok",
            "message": "邮件已发送",
            "to": to,
            "subject": subject,
        }
    
    except Exception as e:
        return {"status": "error", "error": f"发送失败: {e}"}


@tool("read_emails", "读取邮件", timeout=30)
def read_emails(
    folder: str = "INBOX",
    limit: int = 10,
    unread_only: bool = False,
    search: str = "",
) -> dict:
    """
    读取邮件列表。
    
    Args:
        folder: 邮件文件夹
        limit: 返回数量
        unread_only: 是否只读未读邮件
        search: 搜索条件(SIMPLE SEARCH 语法)
    
    Returns:
        邮件列表
    """
    config = EmailConfig()
    
    if not config.is_configured:
        return {"error": "邮件未配置"}
    
    try:
        # 连接 IMAP
        context = ssl.create_default_context()
        
        with imaplib.IMAP4_SSL(config.imap_host, config.imap_port, ssl_context=context) as imap:
            imap.login(config.email, config.password)
            
            # 选择文件夹
            status, data = imap.select(folder, readonly=True)
            if status != "OK":
                return {"error": f"无法打开文件夹: {folder}"}
            
            # 构建搜索条件
            search_criteria = []
            if unread_only:
                search_criteria.append("UNSEEN")
            if search:
                search_criteria.append(f'(OR SUBJECT "{search}" FROM "{search}")')
            
            criteria = " ".join(search_criteria) if search_criteria else "ALL"
            
            # 搜索邮件
            status, message_ids = imap.search(None, criteria)
            if status != "OK":
                return {"error": "搜索失败"}
            
            # 获取邮件 ID 列表
            ids = message_ids[0].split()
            ids = ids[-limit:]  # 取最新的
            ids.reverse()  # 倒序
            
            emails = []
            for msg_id in ids:
                status, msg_data = imap.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    continue
                
                msg = email.message_from_bytes(msg_data[0][1])
                
                email_info = {
                    "id": msg_id.decode(),
                    "from": _decode_header(msg.get("From", "")),
                    "to": _decode_header(msg.get("To", "")),
                    "subject": _decode_header(msg.get("Subject", "")),
                    "date": msg.get("Date", ""),
                    "body": _get_email_body(msg)[:1000],  # 限制长度
                }
                
                # 检查是否已读
                if "(\\Seen)" in str(msg_data[0][0]):
                    email_info["read"] = True
                else:
                    email_info["read"] = False
                
                emails.append(email_info)
            
            return {
                "status": "ok",
                "folder": folder,
                "total": len(ids),
                "emails": emails,
            }
    
    except Exception as e:
        return {"status": "error", "error": f"读取失败: {e}"}


@tool("search_emails", "搜索邮件", timeout=30)
def search_emails(
    query: str,
    folder: str = "INBOX",
    limit: int = 20,
) -> dict:
    """
    搜索邮件。
    
    Args:
        query: 搜索关键词
        folder: 邮件文件夹
        limit: 返回数量
    
    Returns:
        搜索结果
    """
    return read_emails.func(folder=folder, limit=limit, search=query)


@tool("get_email_count", "获取邮件数量统计", timeout=15)
def get_email_count(
    folder: str = "INBOX",
) -> dict:
    """
    获取邮件数量统计。
    
    Args:
        folder: 邮件文件夹
    
    Returns:
        邮件数量
    """
    config = EmailConfig()
    
    if not config.is_configured:
        return {"error": "邮件未配置"}
    
    try:
        context = ssl.create_default_context()
        
        with imaplib.IMAP4_SSL(config.imap_host, config.imap_port, ssl_context=context) as imap:
            imap.login(config.email, config.password)
            
            status, data = imap.select(folder, readonly=True)
            if status != "OK":
                return {"error": f"无法打开文件夹: {folder}"}
            
            total = int(data[0])
            
            # 未读数量
            status, unread_data = imap.search(None, "UNSEEN")
            unread = len(unread_data[0].split()) if unread_data[0] else 0
            
            return {
                "status": "ok",
                "folder": folder,
                "total": total,
                "unread": unread,
                "read": total - unread,
            }
    
    except Exception as e:
        return {"status": "error", "error": f"获取失败: {e}"}


@tool("mark_email_read", "标记邮件为已读", timeout=15)
def mark_email_read(
    email_id: str,
    folder: str = "INBOX",
) -> dict:
    """
    标记邮件为已读。
    
    Args:
        email_id: 邮件 ID
        folder: 邮件文件夹
    
    Returns:
        操作结果
    """
    config = EmailConfig()
    
    if not config.is_configured:
        return {"error": "邮件未配置"}
    
    try:
        context = ssl.create_default_context()
        
        with imaplib.IMAP4_SSL(config.imap_host, config.imap_port, ssl_context=context) as imap:
            imap.login(config.email, config.password)
            imap.select(folder)
            
            # 标记为已读
            status, _ = imap.store(email_id.encode(), "+FLAGS", "\\Seen")
            
            if status == "OK":
                return {"status": "ok", "message": f"邮件 {email_id} 已标记为已读"}
            else:
                return {"status": "error", "error": "标记失败"}
    
    except Exception as e:
        return {"status": "error", "error": f"操作失败: {e}"}


@tool("delete_email", "删除邮件", timeout=15)
def delete_email(
    email_id: str,
    folder: str = "INBOX",
) -> dict:
    """
    删除邮件(移动到已删除)。
    
    Args:
        email_id: 邮件 ID
        folder: 邮件文件夹
    
    Returns:
        操作结果
    """
    config = EmailConfig()
    
    if not config.is_configured:
        return {"error": "邮件未配置"}
    
    try:
        context = ssl.create_default_context()
        
        with imaplib.IMAP4_SSL(config.imap_host, config.imap_port, ssl_context=context) as imap:
            imap.login(config.email, config.password)
            imap.select(folder)
            
            # 标记删除
            status, _ = imap.store(email_id.encode(), "+FLAGS", "\\Deleted")
            
            # 执行删除
            imap.expunge()
            
            if status == "OK":
                return {"status": "ok", "message": f"邮件 {email_id} 已删除"}
            else:
                return {"status": "error", "error": "删除失败"}
    
    except Exception as e:
        return {"status": "error", "error": f"操作失败: {e}"}


@tool("list_email_folders", "列出邮件文件夹", timeout=15)
def list_email_folders() -> dict:
    """
    列出所有邮件文件夹。
    
    Returns:
        文件夹列表
    """
    config = EmailConfig()
    
    if not config.is_configured:
        return {"error": "邮件未配置"}
    
    try:
        context = ssl.create_default_context()
        
        with imaplib.IMAP4_SSL(config.imap_host, config.imap_port, ssl_context=context) as imap:
            imap.login(config.email, config.password)
            
            status, folders = imap.list()
            
            if status == "OK":
                folder_list = []
                for folder in folders:
                    if isinstance(folder, bytes):
                        folder_str = folder.decode()
                        # 解析文件夹名称
                        parts = folder_str.split('" "')
                        if len(parts) >= 2:
                            name = parts[-1].strip('"')
                        else:
                            name = folder_str
                        folder_list.append(name)
                
                return {"status": "ok", "folders": folder_list}
            else:
                return {"status": "error", "error": "获取文件夹失败"}
    
    except Exception as e:
        return {"status": "error", "error": f"获取失败: {e}"}
