# coding:utf-8
# !/usr/bin/python

import logging
import os
from logging import handlers

class Logger(object):
    level_relations = {
        'debug': logging.DEBUG,
        'info': logging.INFO,
        'warning': logging.WARNING,
        'error': logging.ERROR,
        'crit': logging.CRITICAL
    }  #日志级别关系映射

    def __init__(
        self,
        filename,
        level='info',
        when='D',
        backCount=100,
        fmt='%(asctime)s - %(levelname)s - %(filename)s - %(funcName)12s[line:%(lineno)4d]: %(message)8s'
    ):
        self.logger = logging.getLogger(filename)
        format_str = logging.Formatter(fmt, "%Y-%m-%d %H:%M:%S")  #设置日志格式
        self.logger.setLevel(self.level_relations.get(level))  #设置日志级别
        self.logger.propagate = False  # 禁止向 root logger 传播，避免重复输出同一条日志

        abs_filename = os.path.abspath(filename)
        has_same_file_handler = any(
            isinstance(handler, handlers.TimedRotatingFileHandler)
            and getattr(handler, 'baseFilename', None) == abs_filename
            for handler in self.logger.handlers
        )

        if not has_same_file_handler:
            file_handler = handlers.TimedRotatingFileHandler(
                filename=filename,
                when=when,
                backupCount=backCount,
                encoding='utf-8')  #往文件里写入#指定间隔时间自动生成文件的处理器
            #实例化TimedRotatingFileHandler
            #interval是时间间隔，backupCount是备份文件的个数，如果超过这个个数，就会自动删除，when是间隔的时间单位，单位有以下几种：
            # S 秒
            # M 分
            # H 小时、
            # D 天、
            # W 每星期（interval==0时代表星期一）
            # midnight 每天凌晨
            file_handler.setFormatter(format_str)  #设置文件里写入的格式
            self.logger.addHandler(file_handler)

        log_to_console = os.environ.get('LOG_TO_CONSOLE', '1').strip().lower()
        if log_to_console not in ('0', 'false', 'no', 'off'):
            has_stream_handler = any(
                isinstance(handler, logging.StreamHandler)
                and not isinstance(handler, handlers.TimedRotatingFileHandler)
                for handler in self.logger.handlers
            )
            if not has_stream_handler:
                stream_handler = logging.StreamHandler()
                stream_handler.setFormatter(format_str)
                self.logger.addHandler(stream_handler)

    def set_level(self, level):
        self.logger.setLevel(self.level_relations.get(level))  #设置日志级别

