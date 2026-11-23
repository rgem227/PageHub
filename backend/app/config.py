from pathlib import Path
import socket
from urllib.parse import urlparse

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / '.env', extra='ignore')
    environment: str = 'development'
    database_url: str = ''
    secret_key: str = ''
    web_origin: str = 'http://127.0.0.1:5173'
    content_origin: str = 'http://127.0.0.1:8011'
    mcp_public_url: str = ''
    max_upload_mb: int = 20
    max_expanded_mb: int = 100
    max_zip_files: int = 1000
    default_quota_mb: int = 1024
    sms_mode: str = 'development'
    storage_backend: str = 'local'
    s3_endpoint: str = ''
    s3_bucket: str = 'html-hub'
    s3_access_key: str = ''
    s3_secret_key: str = ''
    s3_region: str = 'us-east-1'
    nginx_accel: bool = False

    @property
    def session_cookie_name(self) -> str:
        return 'hub_session' if self.environment == 'production' else 'hub_session_dev'

    @property
    def data_dir(self) -> Path:
        return ROOT / 'data'

    @property
    def mcp_url(self) -> str:
        if self.mcp_public_url:
            parsed = urlparse(self.mcp_public_url)
            if parsed.scheme not in ('http', 'https') or parsed.hostname in ('localhost', '127.0.0.1', '0.0.0.0') or not parsed.hostname:
                raise ValueError('MCP_PUBLIC_URL 必须为可访问的 IP 或域名地址')
            return self.mcp_public_url.rstrip('/') + '/'
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            # 读取默认路由的本地地址，不发送数据包。
            sock.connect(('192.0.2.1', 80))
            address = sock.getsockname()[0]
        if address.startswith('127.') or address == '0.0.0.0':
            raise ValueError('未找到局域网 IP，请设置 MCP_PUBLIC_URL')
        return f'http://{address}:8000/mcp/'

    def validate_runtime(self):
        if not self.database_url or len(self.secret_key) < 32:
            raise RuntimeError('请先执行 backend/bootstrap.py 生成本地配置和数据库')
        if self.environment == 'production':
            if self.sms_mode == 'development':
                raise RuntimeError('生产环境禁止使用开发验证码；需接入真实短信服务')
            if self.sms_mode not in ('provider', 'temporary'):
                raise RuntimeError('生产环境短信模式必须为 provider 或 temporary')
            if self.storage_backend != 's3' or not self.nginx_accel:
                raise RuntimeError('生产环境需配置 S3 和 Nginx 静态资源转发')
            web = urlparse(self.web_origin)
            content = urlparse(self.content_origin)
            web_port = web.port or (443 if web.scheme == 'https' else 80)
            content_port = content.port or (443 if content.scheme == 'https' else 80)
            if (web.scheme.lower(), web.hostname, web_port) == (content.scheme.lower(), content.hostname, content_port):
                raise RuntimeError('生产环境内容地址必须与平台地址属于不同 origin')


settings = Settings()
