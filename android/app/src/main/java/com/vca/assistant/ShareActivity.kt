package com.vca.assistant

import android.content.Intent
import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import org.json.JSONObject

/** 接收系统分享的文本，直接提交到服务端 */
class ShareActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val text = intent?.getStringExtra(Intent.EXTRA_TEXT)
            ?: intent?.getStringExtra(Intent.EXTRA_SUBJECT)
            ?: ""

        if (text.isBlank()) {
            toast("没有收到分享内容")
            finish()
            return
        }

        val server = Prefs.server(this)
        if (server.isEmpty()) {
            toast("请先打开「视频分类助手」填写服务地址")
            finish()
            return
        }

        val url = Net.extractUrl(text)
        if (url == null) {
            toast("这段内容里没有视频链接")
            finish()
            return
        }

        val endpoint = Prefs.withToken(this, "$server/api/tasks")
        Thread {
            val res = Net.postJson(endpoint, JSONObject().put("text", text))
            val err = res.optString("__error", "")
            runOnUiThread {
                if (err.isEmpty()) {
                    val label = res.optString("platform_label", "")
                    toast("已提交到队列${if (label.isNotEmpty()) "：" + label else ""}")
                } else {
                    toast("提交失败：$err")
                }
                finish()
            }
        }.start()
    }
}
