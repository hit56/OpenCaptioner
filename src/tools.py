import os
import re
import sys
import hashlib
import subprocess
import ahocorasick
import pandas as pd

def send_email(message, subject, recipient):
    """
    发送邮件。优先使用 SMTP 环境变量，其次尝试系统 mail 命令。

    环境变量：
    - SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / SMTP_FROM
    - SMTP_SSL=1（默认，端口 465）或 SMTP_TLS=1（端口 587 STARTTLS）
    - SMTP_HTTP_PROXY 或 http_proxy/https_proxy：内网无法直连 SMTP 时走 HTTP CONNECT

    Returns:
        0 on success, -1 on failure
    """
    smtp_host = os.environ.get("SMTP_HOST", "").strip()
    if smtp_host:
        return _send_email_smtp(message, subject, recipient, smtp_host)

    command = f'echo "{message}" | mail -s "{subject}" {recipient}'
    try:
        subprocess.run(command, shell=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return -1
    return 0


def _smtp_proxy_url() -> str:
    for key in ("SMTP_HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return ""


def _open_smtp_socket(smtp_host: str, smtp_port: int, timeout: float = 20):
    """Open a TCP socket to SMTP, optionally via HTTP CONNECT proxy."""
    import base64
    import socket
    from urllib.parse import urlparse

    proxy = _smtp_proxy_url()
    if not proxy:
        return socket.create_connection((smtp_host, smtp_port), timeout=timeout)

    parsed = urlparse(proxy)
    proxy_host = parsed.hostname
    proxy_port = parsed.port or 3120
    if not proxy_host:
        raise RuntimeError(f"invalid SMTP proxy URL: {proxy}")

    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    req = f"CONNECT {smtp_host}:{smtp_port} HTTP/1.1\r\nHost: {smtp_host}:{smtp_port}\r\n"
    if parsed.username is not None:
        token = base64.b64encode(f"{parsed.username}:{parsed.password or ''}".encode()).decode()
        req += f"Proxy-Authorization: Basic {token}\r\n"
    req += "\r\n"
    sock.sendall(req.encode())

    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            break
        response += chunk
    status_line = response.split(b"\r\n", 1)[0].decode("latin1", errors="replace")
    if " 200 " not in f" {status_line} " and not status_line.endswith(" 200"):
        # Accept both "HTTP/1.1 200 Connection established" styles.
        if "200" not in status_line:
            sock.close()
            raise RuntimeError(f"SMTP proxy CONNECT failed: {status_line}")
    return sock


def _send_email_smtp(message, subject, recipient, smtp_host):
    import logging
    import smtplib
    import ssl
    from email.header import Header
    from email.mime.text import MIMEText
    from email.utils import formataddr

    logger = logging.getLogger("smtp_mail")
    smtp_port = int(os.environ.get("SMTP_PORT", "465") or "465")
    smtp_user = os.environ.get("SMTP_USER", "").strip()
    smtp_password = os.environ.get("SMTP_PASSWORD", "").strip()
    smtp_from = os.environ.get("SMTP_FROM", "").strip() or smtp_user or recipient
    use_ssl = os.environ.get("SMTP_SSL", "1").strip().lower() not in {"0", "false", "no"}
    use_tls = os.environ.get("SMTP_TLS", "").strip().lower() in {"1", "true", "yes"}
    if use_tls:
        use_ssl = False

    msg = MIMEText(message, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("智能语音转写", "utf-8")), smtp_from))
    msg["To"] = recipient

    try:
        raw_sock = _open_smtp_socket(smtp_host, smtp_port, timeout=20)
        if use_ssl:
            context = ssl.create_default_context()
            sock = context.wrap_socket(raw_sock, server_hostname=smtp_host)
            server = smtplib.SMTP_SSL()
            server.sock = sock
            server.file = None
            code, resp = server.getreply()
            if code != 220:
                raise smtplib.SMTPConnectError(code, resp)
        else:
            server = smtplib.SMTP()
            server.sock = raw_sock
            server.file = None
            code, resp = server.getreply()
            if code != 220:
                raise smtplib.SMTPConnectError(code, resp)
            server.ehlo()
            if use_tls:
                server.starttls()
                server.ehlo()

        with server:
            if smtp_user:
                server.login(smtp_user, smtp_password)
            server.sendmail(smtp_from, [recipient], msg.as_string())
        return 0
    except Exception as exc:
        logger.warning("SMTP send failed to %s via %s: %s", recipient, smtp_host, exc)
        print(f"[SMTP] send failed to {recipient}: {exc}", flush=True)
        return -1


def encode_url(url):
    # 使用sha256算法生成短的key值
    key = hashlib.sha256(url.encode()).hexdigest()[-8:]  # 选择后8位作为key值
    return key

def loadDict(filename, separator = '', expectN = 1, expect_field_Len = 2, data_type = "float"):
    dict = {}
    for line in open(filename, "r"):
        line = line.rstrip('\r\n')
        if len(line) == 0:
            continue
        if separator == '':
            dict[line] = 0
        else:
            fields = line.split(separator)
            if len(fields) != expect_field_Len:
                sys.stderr.write(line)
                sys.stderr.write('Fields size needs to be %d!\n', expect_field_Len)
                sys.exit()
            key = fields[0]
            value = fields[expectN]
            if len(value) == 0 or value == "null":
                continue
            if data_type == "float":
                dict[key] = float(value)
            else:
                dict[key] = value
    return dict

def is_pure_alpha_num(word):
    if re.match(r"^[()），：；?&=#%_,\- :;/.a-zA-Z0-9']+$", word):
        return True
    else:
        return False

def count_chinese_characters(sentence):
    count = 0
    for char in sentence:
        if '\u4e00' <= char <= '\u9fff':
            count += 1
    return count

def count_english_characters(input_string):
    count = 0
    for char in input_string:
        if char.isalpha():
            count += 1
    return count

def make_clickable(val):
    # target _blank to open new window
    return '<a target="_blank" href="{}">{}</a>'.format(val, val)
 
class AC:
    def __init__(self, words_file_path):
        self.A = ahocorasick.Automaton()
        my_set = set()
        if isinstance(words_file_path, str):
            for line in open(words_file_path, 'r'):
                line = line.strip()
                fields = line.split(':')
                my_set.add(fields[0])
        elif isinstance(words_file_path, list):
            for word in words_file_path:
                my_set.add(word)

        for word in my_set:
            self.A.add_word(word, word)
        self.A.make_automaton()

    def __find_all_match__(self, text):
        result = list(self.A.iter(text))
        return result

    #最长的匹配可能不止一个，所以就返回一个list
    def find_longest_match(self, text):
        result = self.__find_all_match__(text)
        max_length = 0
        max_result = []
        for i in range(0, len(result)):
            if len(result[i][1]) > max_length:
                max_length = len(result[i][1])
                max_result = [result[i][1]]
            elif len(result[i][1]) == max_length:
                max_result.append(result[i][1])
        return max_result

    def is_pure_alpha_num(self, word):
        if re.match(r"[a-zA-Z0-9']+", word):
            return True
        else:
            return False
        
    #按词典进行切割
    def cut(self, text):
        text = text.upper()
        result = []
        res = self.find_longest_match(text)
        if len(res) > 0:
            begin = text.find(res[0])
            end = begin + len(res[0])
            
            # 使用中序遍历方法:左叶子->根节点->右叶子
            #左叶子
            left_res = self.cut(text[0:begin])
            if len(left_res) > 0:
                result.extend(left_res)
            #根节点
            if self.is_pure_alpha_num(res[0]):
                if begin - 1 >= 0 and end < len(text):
                    if self.is_pure_alpha_num(text[begin - 1]) or self.is_pure_alpha_num(text[end]):
                        pass
                    else:
                        result.append(res[0])
            else:
                result.append(res[0])
                
            #右叶子
            right_res = self.cut(text[end:])
            if len(right_res) > 0:
                result.extend(right_res)

        return result
                
    def in_ac(self, word):
        """判断word是否是ac自动机里面的词"""
        if word in self.A:
            return True
        else:
            return False

    def has(self, text):
        """判断text是否包含ac自动机里面的词"""
        if len(self.get_words(text)) == 0:
            return False
        return True
        
    def get_words(self, text):
        """将文本text中含有的ac自动机里面的词全部召回"""
        name_list = self.cut(text)
        return list(name_list)

class Cleaner:
    _instance = None      
    _pattern_url = re.compile("(?P<value>(" +
                             "(?:ftp://|https://|http://|www\\.)" +
                             "[a-zA-Z0-9?%&=#./_!+:\\-\\[\\]~,@;\\*]*\\.[a-zA-Z0-9?%&=#./_!+:\\-\\[\\]~,@;\\*]*?" +
                             "(?=(\\/\\/[@＠][\\u4E00-\\u9FFFa-zA-Z0-9_-]+( )?)|[^a-zA-Z0-9?%&=#./_!+:\\-\\[\\]~,@;]|(&nbsp;)|$)" +
                             ")" +
                             "(\\{([^{\\s]+)\\})?" +
                             ")")

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def matched_stock(self, matched):
        repl = re.findall(self._pattern_url, matched.group('value'))
        return repl[0][-1]
    
    def remove_url(self, text: str)-> str:
        """
        删除文字链接中的url，例如：https://www.taoguba.com.cn/new/stockbar/barRedirect?stockName=%E4%B8%AD%E5%A4%A7%E5%8A%9B%E5%BE%B7{中大力德}
        会被处理成:中大力德
        """
        new_text = re.sub(self._pattern_url, self.matched_stock, text)
        if new_text.startswith('@'):
            return ""
        return new_text
    
    def remove_at_users(self, text: str)-> str:
        """
        如果文字部分@某个用户的话，就一块去除，例如：<a href="http://xueqiu.com/n/后知后觉的群众" target="_blank">@后知后觉的群众</a>
        会被处理成空字符串:""
        """
        pat = re.compile(r'<a href="http://xueqiu.com/n/(.*?)" target="_blank">@\1</a>')
        text = pat.sub('', text)
        return text
    
    def remove_html_tag(self, text:str)-> str:
        '''删除html标签'''
        from zhconv import convert
        from lxml import etree
        if not text:
            return ""
        if not isinstance(text, str):
            text = str(text)
        if not text.strip():
            return ""
        clean_html = etree.HTML(text=text).xpath('string(.)')
        simplified_text = convert(clean_html, "zh-cn")
        return simplified_text 
    
    def remove_dollar_sign(self, text):
        """ 删除贴子的text中挂$的股票标的"""
        stock_list = re.findall(r"\$(.+?)\$", text)
        for stock in stock_list:
            text = text.replace("$" + stock + "$", "")
        return text
    
    def remove_pound_sign(self, text):
        """ 删除贴子的text中挂#的股票标的"""
        stock_list = re.findall(r"#(.+?)#", text)
        for stock in stock_list:
            text = text.replace("#" + stock + "#", "")
        return text
    
    def clean_text(self, text: str)-> str:
        '''删除@用户，删除html标签，删除文字链接中的url，删除用户挂的$股票，删除首尾空白符'''
        '''例如这个帖子：https://xueqiu.com/3899290025/230369134'''
        '''处理结果为：到底是外国的月亮🌙圆？还是外国的和尚会念经？'''
        text = self.remove_at_users(text)
        text = self.remove_html_tag(text)
        text = self.remove_url(text)
        text = self.remove_dollar_sign(text)
        text = text.replace('\n', '')
        text = text.strip()
        return text

cleaner  = Cleaner()
