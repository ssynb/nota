import EventKit
import Flutter
import UIKit

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

/// 系统时区与 EventKit 日历读写，对应 lib/device.dart。
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
    default:
      result(FlutterMethodNotImplemented)
    }
  }

  private func withCalendarAccess(_ result: @escaping FlutterResult, _ body: @escaping () throws -> Any) {
    store.requestFullAccessToEvents { granted, error in
      DispatchQueue.main.async {
        guard granted else {
          result(FlutterError(code: "calendar_denied", message: error?.localizedDescription ?? "未授权访问日历", details: nil))
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
