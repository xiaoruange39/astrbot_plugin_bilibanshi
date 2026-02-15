import asyncio
import random
import re
import json
import os
import subprocess
import uuid
import time
import requests
from pathlib import Path
from typing import List, Dict, Any, Optional
from urllib.parse import quote

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import *

@register("astrbot_plugin_bilibili_polluter", "Xuewu", "B站搬石 - 随机搬视频到群", "1.0.0")
class BilibiliPolluterPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        
        # 默认词库（搜索关键词）
        self.default_keywords = [
            "咕咕嘎嘎", "凑企鹅", "没人看懂", "旮旯给木", "搬石", "高松灯"
        ]
        
        self.config = self._load_config()
        self.running = False
        self.task: Optional[asyncio.Task] = None
        self.temp_files = []
        
        # 请求头（用于搜索）
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://www.bilibili.com/',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        }
        
        # 检查ffmpeg
        self.ffmpeg_available = self._check_ffmpeg()
        if not self.ffmpeg_available:
            logger.warning("FFmpeg未安装，视频合并功能将不可用")
        
        logger.info(f"B站搬石已加载，下载目录: {self.config['download_dir']}")

    async def initialize(self):
        """插件初始化 - 开机自启动"""
        if self.config.get('auto_start', True):
            self.running = True
            self.task = asyncio.create_task(self._timer_task())
            logger.info("B站搬石已开机自启动")

    async def terminate(self):
        """插件卸载时清理"""
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except:
                pass
            self.task = None
        
        # 清理临时文件
        self._cleanup_temp_files()

    def _load_config(self) -> Dict:
        """加载配置"""
        config_path = Path("data") / "plugins" / "bilibili_polluter" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        
        default_config = {
            "auto_start": True,
            "download_dir": str(Path("data") / "plugins" / "bilibili_polluter" / "downloads"),
            "scan_interval": 3600,           # 1小时扫一次
            "target_groups": [],              # 目标群（为空则发所有群）
            "blacklist_groups": [],           # 黑名单群
            "search_keywords": self.default_keywords.copy(),
            "delete_after_send": True,        # 发送后删除
            "max_pages": 5,                   # 搜索页数
            "max_duration": 600,               # 最大时长（秒），默认10分钟=600秒
        }
        
        if config_path.exists():
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    saved_config = json.load(f)
                    default_config.update(saved_config)
            except Exception as e:
                logger.error(f"读取配置文件失败: {e}")
        
        Path(default_config["download_dir"]).mkdir(parents=True, exist_ok=True)
        
        return default_config

    def _save_config(self):
        """保存配置"""
        config_path = Path("data") / "plugins" / "bilibili_polluter" / "config.json"
        try:
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存配置文件失败: {e}")

    def _cleanup_temp_files(self):
        """清理临时文件"""
        for file_path in self.temp_files:
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
            except:
                pass
        self.temp_files.clear()

    def _check_ffmpeg(self) -> bool:
        """检查ffmpeg是否可用"""
        try:
            result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
            return result.returncode == 0
        except:
            return False

    def _clean_filename(self, filename: str) -> str:
        """清理文件名"""
        if not filename:
            return "untitled"
        
        if '《' in filename and '》' in filename:
            title_match = re.search(r'《([^《》]+)》', filename)
            if title_match:
                filename = title_match.group(1)
        
        cleaned = re.sub(r'[<>:"/\\|?*]', '', filename)
        
        if len(cleaned) > 100:
            cleaned = cleaned[:100]
        
        return cleaned.strip()

    # ==================== 搜索部分 ====================

    def _parse_play_count(self, play_text) -> int:
        """将播放量文本转换为数字"""
        if not play_text:
            return 0
        
        if isinstance(play_text, int):
            return play_text
        
        play_text = str(play_text).strip()
        
        unit_multiplier = 1
        if '万' in play_text:
            play_text = play_text.replace('万', '')
            unit_multiplier = 10000
        elif '亿' in play_text:
            play_text = play_text.replace('亿', '')
            unit_multiplier = 100000000
        
        try:
            if '.' in play_text:
                return int(float(play_text) * unit_multiplier)
            else:
                play_text = play_text.replace(',', '')
                numbers = re.findall(r'\d+\.?\d*', play_text)
                if numbers:
                    return int(float(numbers[0]) * unit_multiplier)
                return 0
        except:
            return 0

    def _parse_duration(self, duration_text) -> int:
        """将时长文本转换为秒数"""
        if not duration_text:
            return 0
        
        duration_text = str(duration_text).strip()
        
        try:
            # 处理 "3:45" 或 "1:20:30" 格式
            if ':' in duration_text:
                parts = duration_text.split(':')
                if len(parts) == 2:  # MM:SS
                    return int(parts[0]) * 60 + int(parts[1])
                elif len(parts) == 3:  # HH:MM:SS
                    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            
            # 处理纯数字（秒）
            return int(duration_text)
        except:
            return 0

    def _should_include_video(self, title: str) -> bool:
        """判断视频标题是否包含关键词"""
        if not title:
            return False
        
        title_lower = title.lower()
        for keyword in self.config['search_keywords']:
            if keyword.lower() in title_lower:
                return True
        
        return False

    async def _search_videos_by_keyword(self, keyword: str, max_pages: int = 3) -> List[Dict]:
        """使用指定关键词搜索视频，返回符合时长要求的视频列表"""
        videos = []
        max_duration = self.config.get('max_duration', 600)  # 默认10分钟
        
        logger.info(f"搜索关键词: {keyword}, 最大时长: {max_duration}秒")
        
        for page in range(1, max_pages + 1):
            try:
                # B站搜索API
                api_url = f"https://api.bilibili.com/x/web-interface/search/all/v2?keyword={quote(keyword)}&page={page}"
                
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(
                    None,
                    lambda: requests.get(api_url, headers=self.headers, timeout=10)
                )
                
                if response.status_code != 200:
                    continue
                
                data = response.json()
                
                if data.get('code') != 0:
                    continue
                
                if 'data' not in data:
                    continue
                
                # 查找视频结果
                video_results = None
                for item in data['data'].get('result', []):
                    if item.get('result_type') == 'video':
                        video_results = item.get('data', [])
                        break
                
                if not video_results:
                    break
                
                for video in video_results:
                    try:
                        # 提取标题（去掉em标签）
                        title = video.get('title', '')
                        title = re.sub(r'<[^>]+>', '', title)
                        
                        # 检查标题是否包含关键词
                        if not self._should_include_video(title):
                            continue
                        
                        bvid = video.get('bvid', '')
                        if not bvid:
                            continue
                        
                        # 解析时长
                        duration_text = video.get('duration', '')
                        duration_seconds = self._parse_duration(duration_text)
                        
                        # 检查时长是否超过限制
                        if duration_seconds > max_duration:
                            logger.debug(f"视频时长 {duration_seconds}秒 超过限制 {max_duration}秒，跳过: {title}")
                            continue
                        
                        video_url = f"https://www.bilibili.com/video/{bvid}"
                        
                        # 播放量
                        play_count = self._parse_play_count(video.get('play', 0))
                        
                        # 作者
                        author = video.get('author', '')
                        
                        video_info = {
                            'title': title,
                            'url': video_url,
                            'bvid': bvid,
                            'play_count': play_count,
                            'duration': duration_text,
                            'duration_seconds': duration_seconds,
                            'author': author,
                            'search_keyword': keyword,
                        }
                        
                        videos.append(video_info)
                        logger.debug(f"找到符合时长的视频: {title} ({duration_text})")
                        
                    except Exception as e:
                        continue
                
                # 页间延迟
                if page < max_pages:
                    await asyncio.sleep(random.uniform(0.5, 1))
                    
            except Exception as e:
                logger.error(f"搜索关键词 '{keyword}' 第{page}页出错: {e}")
                break
        
        logger.info(f"关键词 '{keyword}' 搜索完成: 找到 {len(videos)} 个符合时长的视频")
        return videos

    async def _find_random_video(self) -> Optional[Dict]:
        """随机选一个关键词，随机找一个符合时长要求的视频"""
        keywords = self.config.get('search_keywords', self.default_keywords)
        
        if not keywords:
            logger.error("没有搜索关键词")
            return None
        
        max_duration = self.config.get('max_duration', 600)
        max_attempts = 10  # 最大尝试次数，避免死循环
        
        for attempt in range(max_attempts):
            # 随机选一个关键词
            keyword = random.choice(keywords)
            logger.info(f"第{attempt+1}次尝试，选中关键词: {keyword}")
            
            # 随机选页数（1-3页）
            pages = random.randint(1, 3)
            
            # 搜索该关键词
            videos = await self._search_videos_by_keyword(keyword, pages)
            
            if not videos:
                logger.info(f"关键词 '{keyword}' 没有找到符合时长的视频")
                continue
            
            # 随机选一个视频
            selected = random.choice(videos)
            duration = selected.get('duration_seconds', 0)
            
            logger.info(f"随机选中视频: {selected['title']} (时长: {selected['duration']}, {duration}秒)")
            
            # 二次确认时长（虽然搜索时已经过滤，但再检查一下）
            if duration <= max_duration:
                return selected
            else:
                logger.info(f"视频时长 {duration}秒 超过限制，重新选择")
                continue
        
        logger.error(f"尝试 {max_attempts} 次后仍未找到合适的视频")
        return None

    # ==================== 下载部分 ====================

    async def _get_video_urls(self, bvid: str):
        """获取视频和音频URL"""
        url = f"https://www.bilibili.com/video/{bvid}"
        
        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.bilibili.com/'
        })
        
        try:
            loop = asyncio.get_event_loop()
            
            # 1. 先发HEAD请求获取最终URL（处理重定向）
            response = await loop.run_in_executor(
                None,
                lambda: session.head(url, allow_redirects=True, timeout=10)
            )
            final_url = response.url
            
            # 2. 获取页面内容
            response = await loop.run_in_executor(
                None,
                lambda: session.get(final_url, timeout=10)
            )
            html_content = response.text
            
            # 3. 提取 window.__playinfo__
            playinfo_match = re.search(r'window\.__playinfo__=(.*?)</script>', html_content)
            if not playinfo_match:
                logger.error(f"未找到playinfo: {bvid}")
                return None, None
            
            playinfo = json.loads(playinfo_match.group(1))
            
            # 4. 解析视频和音频URL
            if 'data' in playinfo and 'dash' in playinfo['data']:
                video_qualities = playinfo['data']['dash'].get('video', [])
                audio_qualities = playinfo['data']['dash'].get('audio', [])
                
                if video_qualities and audio_qualities:
                    # 选最高画质
                    best_video = max(video_qualities, key=lambda x: x.get('bandwidth', 0))
                    best_audio = max(audio_qualities, key=lambda x: x.get('bandwidth', 0))
                    
                    video_url = best_video.get('baseUrl')
                    audio_url = best_audio.get('baseUrl')
                    
                    return video_url, audio_url
            
            return None, None
            
        except Exception as e:
            logger.error(f"解析视频URL失败 {bvid}: {e}")
            return None, None

    async def _download_file(self, url: str, output_path: str, progress_callback=None) -> bool:
        """下载文件"""
        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.bilibili.com/'
        })
        
        try:
            loop = asyncio.get_event_loop()
            
            def download():
                response = session.get(url, stream=True, timeout=30)
                if response.status_code != 200:
                    return False
                
                total_size = int(response.headers.get('content-length', 0))
                downloaded_size = 0
                
                with open(output_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            downloaded_size += len(chunk)
                            if total_size > 0 and progress_callback:
                                progress = (downloaded_size / total_size) * 100
                                progress_callback(progress)
                
                return True
            
            return await loop.run_in_executor(None, download)
            
        except Exception as e:
            logger.error(f"下载出错: {e}")
            return False

    async def _merge_video_audio(self, video_path: str, audio_path: str, output_path: str) -> bool:
        """合并视频和音频"""
        try:
            cmd = f'ffmpeg -i "{video_path}" -i "{audio_path}" -c:v copy -c:a aac "{output_path}" -y -hide_banner -loglevel error'
            
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode == 0:
                # 成功时删除临时文件
                for path in [video_path, audio_path]:
                    if os.path.exists(path):
                        os.remove(path)
                return True
            else:
                logger.error(f"合并失败: {stderr.decode()[:200]}")
                return False
                
        except Exception as e:
            logger.error(f"合并出错: {e}")
            return False

    async def _download_video(self, video_info: Dict) -> Optional[str]:
        """下载单个视频"""
        bvid = video_info.get('bvid', '')
        title = video_info.get('title', '未知标题')
        
        if not bvid:
            logger.error("没有bvid，无法下载")
            return None
        
        try:
            # 清理文件名
            cleaned_title = self._clean_filename(title)
            unique_id = str(uuid.uuid4())[:8]
            
            # 临时文件路径
            video_temp = os.path.join(self.config["download_dir"], f"video_{unique_id}.m4s")
            audio_temp = os.path.join(self.config["download_dir"], f"audio_{unique_id}.m4s")
            output_path = os.path.join(self.config["download_dir"], f"{cleaned_title}_{unique_id}.mp4")
            
            self.temp_files.extend([video_temp, audio_temp, output_path])
            
            # 获取视频和音频URL
            logger.info(f"获取下载地址: {bvid}")
            video_url, audio_url = await self._get_video_urls(bvid)
            if not video_url:
                logger.error(f"获取下载地址失败: {bvid}")
                return None
            
            # 下载视频
            logger.info(f"下载视频: {title}")
            video_success = await self._download_file(video_url, video_temp)
            if not video_success:
                logger.error(f"视频下载失败: {bvid}")
                return None
            
            # 下载音频
            logger.info(f"下载音频: {title}")
            audio_success = await self._download_file(audio_url, audio_temp)
            if not audio_success:
                logger.error(f"音频下载失败: {bvid}")
                return None
            
            # 合并
            if self.ffmpeg_available:
                logger.info(f"合并视频音频: {title}")
                merge_success = await self._merge_video_audio(video_temp, audio_temp, output_path)
                if merge_success:
                    logger.info(f"下载完成: {output_path}")
                    return output_path
                else:
                    return None
            else:
                logger.error("FFmpeg不可用，无法合并")
                return None
                
        except Exception as e:
            logger.error(f"下载视频异常: {e}")
            return None

    # ==================== 发送部分（修复版）====================

    async def _get_all_groups(self) -> List[str]:
        """获取所有群ID"""
        try:
            # 尝试从context获取群列表
            groups = await self.context.get_all_groups()
            if groups:
                return [str(g.id) for g in groups]
        except Exception as e:
            logger.error(f"获取群列表失败: {e}")
        
        # 如果无法获取，返回空列表
        return []

    async def _send_to_current_chat(self, event: AstrMessageEvent, file_path: str, video_info: Dict):
        """发送到当前聊天（用于 /bilibanshi now 命令）"""
        title = video_info.get('title', '未知标题')
        author = video_info.get('author', '未知UP主')
        play = video_info.get('play_count', 0)
        bvid = video_info.get('bvid', '')
        duration = video_info.get('duration', '未知')
        
        # 文字消息
        text_msg = (
            f"【B站搬石】\n"
            f"标题：{title}\n"
            f"UP主：{author}\n"
            f"时长：{duration}\n"
            f"播放量：{play}\n"
            f"链接：https://www.bilibili.com/video/{bvid}"
        )
        
        try:
            # 构建消息链
            chain = [Plain(text_msg)]
            
            # 检查文件是否存在
            if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                video = Video.fromFileSystem(path=file_path)
                chain.append(video)
                logger.info(f"添加视频到消息链: {file_path}")
            
            # 使用 event.chain_result 发送（因为是在命令中）
            yield event.chain_result(chain)
            logger.info("已发送到当前聊天")
            
        except Exception as e:
            logger.error(f"发送到当前聊天失败: {e}")
            yield event.plain_result(f"发送失败: {e}")

    async def _send_to_all_groups(self, file_path: str, video_info: Dict):
        """发送到所有群（用于定时任务）"""
        # 获取所有群
        all_groups = await self._get_all_groups()
        blacklist = [str(b) for b in self.config.get('blacklist_groups', [])]
        
        if not all_groups:
            logger.warning("没有获取到任何群")
            return
        
        # 过滤黑名单
        groups_to_send = [g for g in all_groups if g not in blacklist]
        
        if not groups_to_send:
            logger.warning("所有群都在黑名单中，无法发送")
            return
        
        title = video_info.get('title', '未知标题')
        author = video_info.get('author', '未知UP主')
        play = video_info.get('play_count', 0)
        bvid = video_info.get('bvid', '')
        duration = video_info.get('duration', '未知')
        
        # 文字消息
        text_msg = (
            f"【B站搬石-定时任务】\n"
            f"标题：{title}\n"
            f"UP主：{author}\n"
            f"时长：{duration}\n"
            f"播放量：{play}\n"
            f"链接：https://www.bilibili.com/video/{bvid}"
        )
        
        # 检查文件
        file_exists = os.path.exists(file_path) and os.path.getsize(file_path) > 0
        
        for group_id in groups_to_send:
            try:
                # 构建消息链
                chain = [Plain(text_msg)]
                
                if file_exists:
                    video = Video.fromFileSystem(path=file_path)
                    chain.append(video)
                
                # 定时任务中使用 context.send_message
                # 注意：这里假设 group_id 可以直接作为 unified_msg_origin
                # 如果不行，需要先获取 unified_msg_origin
                await self.context.send_message(group_id, chain)
                logger.info(f"已发送到群 {group_id}")
                
                # 群之间延迟
                await asyncio.sleep(2)
                
            except Exception as e:
                logger.error(f"发送到群 {group_id} 失败: {e}")

    # ==================== 主流程（修改版）====================

    async def _scan_and_download(self, event: AstrMessageEvent = None):
        """扫描并随机下载一个视频
        Args:
            event: 如果有 event，说明是 /bilibanshi now 命令触发的，发送到当前聊天
                   如果没有 event，说明是定时任务，发送到所有群
        """
        logger.info("开始随机搬石...")
        
        # 随机找一个符合时长的视频
        target = await self._find_random_video()
        
        if not target:
            logger.error("没有找到合适的视频")
            if event:
                yield event.plain_result("❌ 没有找到合适的视频")
            return
        
        logger.info(f"选中视频: {target['title']} (BV: {target['bvid']}, 时长: {target['duration']})")
        
        if event:
            yield event.plain_result(f"✅ 选中视频: {target['title']}\n⏳ 开始下载...")
        
        # 下载
        file_path = await self._download_video(target)
        
        if file_path and os.path.exists(file_path):
            logger.info(f"下载成功: {file_path}")
            
            if event:
                # /bilibanshi now 命令：发送到当前聊天
                async for result in self._send_to_current_chat(event, file_path, target):
                    yield result
            else:
                # 定时任务：发送到所有群
                await self._send_to_all_groups(file_path, target)
            
            # 发送后删除
            if self.config.get('delete_after_send', True):
                try:
                    os.remove(file_path)
                    logger.info(f"已删除文件: {file_path}")
                except Exception as e:
                    logger.error(f"删除文件失败: {e}")
        else:
            logger.error("下载失败")
            if event:
                yield event.plain_result("❌ 下载失败")

    async def _timer_task(self):
        """定时任务"""
        while self.running:
            try:
                # 定时任务不传 event，会发送到所有群
                await self._scan_and_download()
            except Exception as e:
                logger.error(f"定时任务异常: {e}")
            
            await asyncio.sleep(self.config['scan_interval'])

    # ==================== 指令区（保持原有格式）====================

    @filter.command("bilibanshi on")
    async def turn_on(self, event: AstrMessageEvent):
        """开启搬石"""
        if self.running:
            yield event.plain_result("搬石已经在运行了")
            return
        
        self.running = True
        self.task = asyncio.create_task(self._timer_task())
        yield event.plain_result("B站搬石已开启")

    @filter.command("bilibanshi off")
    async def turn_off(self, event: AstrMessageEvent):
        """关闭搬石"""
        if not self.running:
            yield event.plain_result("搬石已经关闭了")
            return
        
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except:
                pass
            self.task = None
        yield event.plain_result("B站搬石已关闭")

    @filter.command("bilibanshi now")
    async def scan_now(self, event: AstrMessageEvent):
        """立即执行一次（发送到当前聊天）"""
        yield event.plain_result("开始搬石...")
        # 传入 event，会发送到当前聊天
        async for result in self._scan_and_download(event):
            yield result

    @filter.command("bilibanshi list")
    async def list_status(self, event: AstrMessageEvent):
        """查看当前状态"""
        max_duration = self.config.get('max_duration', 600)
        
        # 获取所有群
        all_groups = await self._get_all_groups()
        
        status = [
            "=== B站搬石状态 ===",
            f"运行状态: {'✅ 运行中' if self.running else '❌ 已停止'}",
            f"扫描间隔: {self.config['scan_interval']}秒",
            f"最大时长: {max_duration}秒 ({max_duration//60}分钟)",
            f"机器人加入的群: {len(all_groups)} 个",
            f"黑名单群: {len(self.config.get('blacklist_groups', []))} 个",
            f"关键词: {len(self.config.get('search_keywords', self.default_keywords))} 个",
            f"下载目录: {self.config['download_dir']}",
            f"FFmpeg: {'✅ 可用' if self.ffmpeg_available else '❌ 不可用'}",
        ]
        yield event.plain_result("\n".join(status))

    @filter.command("bilibanshi h")
    async def add_blacklist(self, event: AstrMessageEvent):
        """添加黑名单群: /bilibanshi h 123456"""
        parts = event.message_str.strip().split()
        if len(parts) < 2:
            yield event.plain_result("用法: /bilibanshi h <群号>")
            return
        
        group_id = parts[1]
        if not group_id.isdigit():
            yield event.plain_result("群号必须是数字")
            return
        
        if 'blacklist_groups' not in self.config:
            self.config['blacklist_groups'] = []
        
        if group_id not in self.config['blacklist_groups']:
            self.config['blacklist_groups'].append(group_id)
            self._save_config()
            yield event.plain_result(f"已添加黑名单群: {group_id}")
        else:
            yield event.plain_result("该群已在黑名单中")

    @filter.command("bilibanshi -h")
    async def remove_blacklist(self, event: AstrMessageEvent):
        """移除黑名单群: /bilibanshi -h 123456"""
        parts = event.message_str.strip().split()
        if len(parts) < 2:
            yield event.plain_result("用法: /bilibanshi -h <群号>")
            return
        
        group_id = parts[1]
        if 'blacklist_groups' in self.config and group_id in self.config['blacklist_groups']:
            self.config['blacklist_groups'].remove(group_id)
            self._save_config()
            yield event.plain_result(f"已移除黑名单群: {group_id}")
        else:
            yield event.plain_result("该群不在黑名单中")

    @filter.command("bilibanshi c")
    async def add_keyword(self, event: AstrMessageEvent):
        """添加搜索关键词: /bilibanshi c 搞笑"""
        parts = event.message_str.strip().split()
        if len(parts) < 2:
            yield event.plain_result("用法: /bilibanshi c <关键词>")
            return
        
        keyword = ' '.join(parts[1:])  # 支持带空格的关键词
        if 'search_keywords' not in self.config:
            self.config['search_keywords'] = self.default_keywords.copy()
        
        if keyword not in self.config['search_keywords']:
            self.config['search_keywords'].append(keyword)
            self._save_config()
            yield event.plain_result(f"已添加关键词: {keyword}")
        else:
            yield event.plain_result("该关键词已存在")

    @filter.command("bilibanshi del")
    async def remove_keyword(self, event: AstrMessageEvent):
        """删除搜索关键词: /bilibanshi del 搞笑"""
        parts = event.message_str.strip().split()
        if len(parts) < 2:
            yield event.plain_result("用法: /bilibanshi del <关键词>")
            return
        
        keyword = ' '.join(parts[1:])
        if 'search_keywords' in self.config and keyword in self.config['search_keywords']:
            self.config['search_keywords'].remove(keyword)
            self._save_config()
            yield event.plain_result(f"已删除关键词: {keyword}")
        else:
            yield event.plain_result("未找到该关键词")

    @filter.command("bilibanshi interval")
    async def set_interval(self, event: AstrMessageEvent):
        """设置扫描间隔(秒): /bilibanshi interval 3600"""
        parts = event.message_str.strip().split()
        if len(parts) < 2:
            yield event.plain_result("用法: /bilibanshi interval <秒数>")
            return
        
        try:
            interval = int(parts[1])
            if interval < 60:
                yield event.plain_result("间隔不能小于60秒")
                return
            
            self.config['scan_interval'] = interval
            self._save_config()
            yield event.plain_result(f"已设置扫描间隔: {interval}秒")
        except ValueError:
            yield event.plain_result("请输入有效的数字")

    @filter.command("bilibanshi maxduration")
    async def set_max_duration(self, event: AstrMessageEvent):
        """设置最大时长（秒）: /bilibanshi maxduration 600"""
        parts = event.message_str.strip().split()
        if len(parts) < 2:
            yield event.plain_result("用法: /bilibanshi maxduration <秒数>")
            return
        
        try:
            duration = int(parts[1])
            if duration < 10:
                yield event.plain_result("时长不能小于10秒")
                return
            
            self.config['max_duration'] = duration
            self._save_config()
            yield event.plain_result(f"已设置最大时长: {duration}秒 ({duration//60}分钟)")
        except ValueError:
            yield event.plain_result("请输入有效的数字")