package com.vca.assistant

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.media.projection.MediaProjectionManager
import android.content.pm.PackageManager
import android.graphics.Typeface
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.text.InputType
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import org.json.JSONArray
import org.json.JSONObject

class MainActivity : AppCompatActivity() {

    companion object {
        private const val REQUEST_WATCH = 42
        private const val REQUEST_WATCH_AUDIO = 43
    }

    private lateinit var serverInput: EditText
    private lateinit var tokenInput: EditText
    private lateinit var statusText: TextView

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val pad = dp(20)
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
        }

        root.addView(TextView(this).apply {
            text = "视频分类助手"
            textSize = 24f
            setTypeface(typeface, Typeface.BOLD)
        })
        root.addView(TextView(this).apply {
            text = "填写服务地址后即可使用控制台；开启无障碍后，刷视频时会自动提示分类。"
            textSize = 13f
            setTextColor(0xFF6B7385.toInt())
            setPadding(0, dp(6), 0, dp(16))
        })

        root.addView(label("服务地址"))
        serverInput = EditText(this).apply {
            hint = "https://xxxx.hf.space 或 http://192.168.1.5:8000"
            setText(Prefs.server(this@MainActivity))
            inputType = InputType.TYPE_TEXT_VARIATION_URI
            setSingleLine()
        }
        root.addView(serverInput)

        root.addView(label("访问口令（服务端未设置可留空）"))
        tokenInput = EditText(this).apply {
            hint = "config.yaml 里的 access_key"
            setText(Prefs.token(this@MainActivity))
            setSingleLine()
        }
        root.addView(tokenInput)

        root.addView(action("保存并测试连接") { saveAndTest() })
        root.addView(action("打开控制台") { onOpenConsole() })
        root.addView(action("开始跟看 60 秒") { beginWatch() })
        root.addView(action("开启无障碍自动识别") {
            try {
                startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
            } catch (e: Exception) {
                toast("无法打开无障碍设置，请手动到系统设置中开启")
            }
        })
        root.addView(action("分享菜单怎么用") { showHelp() })

        statusText = TextView(this).apply {
            textSize = 13f
            setTextColor(0xFF6B7385.toInt())
            setPadding(0, dp(16), 0, 0)
            text = "状态：未连接"
        }
        root.addView(statusText)

        setContentView(ScrollView(this).apply { addView(root) })

        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
    }

    private fun label(text: String): TextView = TextView(this).apply {
        this.text = text
        textSize = 12f
        setTextColor(0xFF6B7385.toInt())
        setPadding(0, dp(10), 0, dp(4))
    }

    private fun action(text: String, onClick: () -> Unit): Button = Button(this).apply {
        this.text = text
        isAllCaps = false
        setOnClickListener { onClick() }
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT
        ).apply { topMargin = dp(10) }
    }

    private fun currentServer(): String? {
        val server = serverInput.text.toString().trim().trimEnd('/')
        if (!server.startsWith("http://") && !server.startsWith("https://")) {
            toast("地址需要以 http:// 或 https:// 开头")
            return null
        }
        Prefs.setServer(this, server)
        Prefs.setToken(this, tokenInput.text.toString().trim())
        return server
    }

    private fun saveAndTest() {
        val server = currentServer() ?: return
        statusText.text = "状态：正在连接…"
        Thread {
            val body = JSONObject().put("title", "测试连接").put("tags", JSONArray())
            val res = Net.postJson(Prefs.withToken(this, "$server/api/classify"), body)
            val err = res.optString("__error", "")
            runOnUiThread {
                statusText.text = if (err.isEmpty()) {
                    "状态：连接正常，分类引擎可用"
                } else {
                    "状态：连接失败 · $err"
                }
            }
        }.start()
    }

    private fun onOpenConsole() {
        val server = currentServer() ?: return
        startActivity(Intent(this, WebActivity::class.java).putExtra("server", server))
    }

    private fun beginWatch() {
        currentServer() ?: return
        if (Build.VERSION.SDK_INT >= 29 &&
            checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), REQUEST_WATCH_AUDIO)
            return
        }
        requestProjection()
    }

    private fun requestProjection() {
        val manager = getSystemService(MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        @Suppress("DEPRECATION")
        startActivityForResult(manager.createScreenCaptureIntent(), REQUEST_WATCH)
    }

    @Suppress("DEPRECATION")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQUEST_WATCH) return
        if (resultCode != Activity.RESULT_OK || data == null) {
            toast("未授权录屏，跟看未启动")
            return
        }
        val audioAllowed = Build.VERSION.SDK_INT >= 29 &&
            checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
        val intent = Intent(this, FollowWatchService::class.java)
            .setAction(FollowWatchService.ACTION_START)
            .putExtra(FollowWatchService.EXTRA_RESULT_CODE, resultCode)
            .putExtra(FollowWatchService.EXTRA_PROJECTION_DATA, data)
            .putExtra(FollowWatchService.EXTRA_AUDIO_ALLOWED, audioAllowed)
        startForegroundService(intent)
        toast("请返回视频 App，跟看期间可在通知栏停止")
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQUEST_WATCH_AUDIO) {
            if (grantResults.firstOrNull() != PackageManager.PERMISSION_GRANTED) {
                toast("未授予内录权限，将仅分析画面和文字")
            }
            requestProjection()
        }
    }

    private fun showHelp() {
        AlertDialog.Builder(this)
            .setTitle("分享菜单用法")
            .setMessage(
                "在抖音、头条、快手、B站等 App 里点「分享」→「更多」→ 选择「视频分类助手」，" +
                    "链接会自动提交到你的服务端，按你设置的开关完成去水印、字幕和配音。\n\n" +
                    "如果分享列表里没有本应用，先在系统设置里确认没有禁用它的分享入口。"
            )
            .setPositiveButton("知道了", null)
            .show()
    }
}
