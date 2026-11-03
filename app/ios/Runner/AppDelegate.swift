import EventKit
import Flutter
import UIKit
import UserNotifications

@main
@objc class AppDelegate: FlutterAppDelegate, FlutterImplicitEngineDelegate {
  private let device = NotaDevice()

  override func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
  ) -> Bool {
    return super.application(application, didFinishLaunchingWithOptions: launchOptions)
  }

  func didInitializeImplicitFlutterEngine(_ engineBridge: FlutterImplicitEngineBridge) {
    GeneratedPluginRegistrant.register(with: engineBridge.pluginRegistry)
    let registrar = engineBridge.pluginRegistry.registrar(forPlugin: "NotaDevice")!
    let channel = FlutterMethodChannel(name: "nota/device", binaryMessenger: registrar.messenger())
    channel.setMethodCallHandler(device.handle)
  }
}

/// 系统时区、EventKit 日历读写、本地通知，对应 lib/device.dart。
/// 权限都在第一次真正用到时申请：日历在第一次执行日历动作时，通知在第一次开始录音时。
final class NotaDevice {
  private let store = EKEventStore()

  func handle(_ call: FlutterMethodCall, result: @escaping FlutterResult) {
    let args = call.arguments as? [String: Any] ?? [:]
    switch call.method {
    case "timezone":
      result(TimeZone.current.identifier)
    case "createEvent":
      withCalendarAccess(result) { try self.createEvent(args) }
    case "deleteEvent":
      withCalendarAccess(result) { try self.deleteEvent(args["id"] as! String) }
    case "openSettings":
      UIApplication.shared.open(URL(string: UIApplication.openSettingsURLString)!)
      result(nil)
    case "requestNotifications":
      UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { granted, _ in
        DispatchQueue.main.async { result(granted) }
      }
    case "notify":
      let content = UNMutableNotificationContent()
      content.title = args["title"] as! String
      content.body = args["body"] as! String
      content.sound = .default
      let id = args["id"] as! String
      UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: id, content: content, trigger: nil))
      result(nil)
    case "clearNotification":
      UNUserNotificationCenter.current().removeDeliveredNotifications(withIdentifiers: [args["id"] as! String])
      result(nil)
    default:
      result(FlutterMethodNotImplemented)
    }
  }

  private func withCalendarAccess(_ result: @escaping FlutterResult, _ body: @escaping () throws -> Any) {
    store.requestFullAccessToEvents { granted, error in
      DispatchQueue.main.async {
        guard granted else {
          result(FlutterError(code: "calendar_denied", message: "未授权日历：请在 设置 > 隐私与安全性 > 日历 中允许 Nota 完全访问", details: nil))
          return
        }
        do {
          result(try body())
        } catch {
          result(FlutterError(code: "calendar_failed", message: error.localizedDescription, details: nil))
        }
      }
    }
  }

  private func createEvent(_ args: [String: Any]) throws -> String {
    let event = EKEvent(eventStore: store)
    event.calendar = store.defaultCalendarForNewEvents
    event.title = args["title"] as? String
    event.startDate = Date(timeIntervalSince1970: (args["startMs"] as! NSNumber).doubleValue / 1000)
    event.endDate = Date(timeIntervalSince1970: (args["endMs"] as! NSNumber).doubleValue / 1000)
    event.location = args["location"] as? String
    if let minutes = args["alarmMinutesBefore"] as? Int {
      event.addAlarm(EKAlarm(relativeOffset: TimeInterval(-minutes * 60)))
    }
    try store.save(event, span: .thisEvent, commit: true)
    return event.eventIdentifier
  }

  private func deleteEvent(_ id: String) throws -> Bool {
    guard let event = store.event(withIdentifier: id) else { return false }
    try store.remove(event, span: .thisEvent, commit: true)
    return true
  }
}
