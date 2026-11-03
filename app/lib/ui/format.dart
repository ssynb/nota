import 'package:flutter/material.dart';

import '../api.dart';

const recordingStatusLabel = {
  'recording': '录音中',
  'uploading': '上传中',
  'transcribing': '转写中',
  'summarizing': '整理纪要',
  'done': '已完成',
  'failed': '处理失败',
  'paused_quota': '录音已保存，等待整理',
};

const actionStatusLabel = {
  'pending_confirmation': '待确认',
  'pending': '待执行',
  'done': '已完成',
  'failed': '失败',
  'rejected': '已拒绝',
};

const actionErrorLabel = {
  'calendar_denied': '未授权日历，请在设置中允许 Nota 完全访问日历',
  'calendar_failed': '写入系统日历失败',
  'event_not_found': '日历里已找不到这条日程',
  'user_cancelled': '你取消了分享',
  'unsupported_type': '手机不支持执行此类动作',
};

String two(int n) => n.toString().padLeft(2, '0');

/// ISO 8601 → 本地时间“11月3日 09:30”。
String dateTime(String? iso) {
  if (iso == null) return '';
  final t = DateTime.parse(iso).toLocal();
  return '${t.month}月${t.day}日 ${two(t.hour)}:${two(t.minute)}';
}

String clock(num seconds) {
  final s = seconds.round();
  return '${two(s ~/ 60)}:${two(s % 60)}';
}

String minutesLabel(int minutes) {
  if (minutes % 1440 == 0) return '${minutes ~/ 1440} 天';
  if (minutes % 60 == 0) return '${minutes ~/ 60} 小时';
  return '$minutes 分钟';
}

/// 动作的人话标题与说明。
(String, String) describeAction(Map<String, dynamic> a) {
  final args = (a['args'] as Map?)?.cast<String, dynamic>() ?? const {};
  switch (a['type']) {
    case 'create_calendar_event':
      final alarm = args['alarm_minutes_before'] as int?;
      return (
        '建日程：${args['title']}',
        [
          '${dateTime(args['start'] as String?)} – ${dateTime(args['end'] as String?)}',
          if (args['location'] != null) args['location'],
          if (alarm != null) '提前 ${minutesLabel(alarm)}提醒',
        ].join(' · '),
      );
    case 'delete_calendar_event':
      return ('删除日程', '撤销之前建的系统日程');
    case 'share_message':
      return ('发消息给 ${args['to']}', '${args['text']}');
    default:
      return ('${a['type']}', '手机不支持执行此类动作');
  }
}

/// 把异常翻成给用户看的一句话。
String errorText(Object e) {
  if (e is ApiException) {
    return switch (e.reason) {
      'ask_quota' => '本月提问额度已用完',
      final String reason => reason,
      null => '服务器返回 ${e.status}：${e.body}',
    };
  }
  return '连不上服务器：$e';
}

void showError(BuildContext context, Object e) =>
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(errorText(e))));
