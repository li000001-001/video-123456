package com.vca.assistant

import android.accessibilityservice.AccessibilityService
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import org.json.JSONObject

/** 跟看服务只复用屏幕文字与包名，不改变原有自动分类逻辑。 */
data class AssistContext(val packageName: String, val text: String, val capturedAt: Long)

/**
 * 无障碍服务：读取当前页面的文字（视频标题等），交给服务端分类，结果用通知提示。
 * 屏幕采集与内录只由用户手动启动的 FollowWatchService 执行。
 */
class AssistService : AccessibilityService() {

    companion object {
        @Volatile
        private var latestContext: AssistContext? = null

        private val watchedPackages = setOf(
            "com.ss.android.ugc.aweme",      // 抖音
            "com.ss.android.article.news",   // 今日头条
            "com.ss.android.article.lite",   // 头条极速版
            "com.smile.gifmaker",            // 快手
            "com.kuaishou.nebula",           // 快手极速版
            "tv.danmaku.bili",               // 哔哩哔哩
        )

        fun currentContext(): AssistContext? = latestContext

        fun isWatchedPackage(packageName: String): Boolean = packageName in watchedPackages
    }

    private var lastText = ""
    private var lastAt = 0L

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        val e = event ?: return
        val pkg = e.packageName?.toString() ?: return
        if (pkg !in watchedPackages) return

        val now = System.currentTimeMillis()
        // 节流：2.5 秒内不重复处理，避免滚动时频繁请求
        if (now - lastAt < 2500) return

        val text = runCatching { collectText(rootInActiveWindow) }.getOrNull() ?: return
        if (text.length < 2) return
        latestContext = AssistContext(pkg, text.take(4000), now)
        if (text.length < 6 || text == lastText) return

        lastAt = now
        lastText = text
        report(pkg, text)
    }

    private fun collectText(root: AccessibilityNodeInfo?): String? {
        if (root == null) return null
        val sb = StringBuilder()
        val queue = ArrayDeque<AccessibilityNodeInfo>()
        queue.addLast(root)
        var visited = 0
        while (queue.isNotEmpty() && visited < 80) {
            val node = queue.removeFirst()
            visited++
            val t = node.text?.toString()?.trim()
            val d = node.contentDescription?.toString()?.trim()
            if (!t.isNullOrEmpty() && t.length >= 2) {
                sb.append(t).append('\n')
            } else if (!d.isNullOrEmpty() && d.length >= 2) {
                sb.append(d).append('\n')
            }
            for (i in 0 until node.childCount) {
                node.getChild(i)?.let { queue.addLast(it) }
            }
        }
        return sb.toString().trim().ifEmpty { null }
    }

    private fun report(pkg: String, text: String) {
        val server = Prefs.server(this)
        if (server.isEmpty()) return
        val endpoint = Prefs.withToken(this, "$server/api/bridge/report")
        val body = JSONObject()
            .put("title", text.take(200))
            .put("package", pkg)
            .put("token", Prefs.token(this))

        Thread {
            val res = Net.postJson(endpoint, body)
            if (res.optString("__error", "").isNotEmpty()) return@Thread
            val category = res.optString("category", "")
            if (category.isEmpty() || category == "其他") return@Thread
            notifyResult(res.optString("emoji", ""), category, text)
        }.start()
    }

    private fun notifyResult(emoji: String, category: String, text: String) {
        val nm = getSystemService(NOTIFICATION_SERVICE) as? NotificationManager ?: return
        val channelId = "vca_classify"
        if (nm.getNotificationChannel(channelId) == null) {
            nm.createNotificationChannel(
                NotificationChannel(channelId, "分类结果", NotificationManager.IMPORTANCE_LOW)
            )
        }
        val notification: Notification = Notification.Builder(this, channelId)
            .setContentTitle("$emoji 识别为：$category")
            .setContentText(text.lineSequence().firstOrNull()?.take(50) ?: "")
            .setSmallIcon(android.R.drawable.ic_menu_info_details)
            .setAutoCancel(true)
            .build()
        runCatching { nm.notify(1001, notification) }
    }

    override fun onInterrupt() {
        // 无需处理
    }
}
