"""jxut-seat-grabber —— 图书馆座位预约自动化工具包。

接口逆向自 jxut.educationgroup.cn.har：
    POST /tsg/kzwWx/getSjd  {rq}                  -> 当日时段列表
    POST /tsg/kzwWx/getZws  {rq, sjdId}           -> 各房间空闲座位
    POST /tsg/kzwWx/save    {rq, sjdId, zwId}     -> 预约 / 换座
"""

__version__ = "1.0.0"
