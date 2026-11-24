package com.vca.assistant

import android.os.Bundle
import android.view.ViewGroup
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.appcompat.app.AppCompatActivity

/** 用 WebView 承载控制台页面，等同于把网页版装进 App */
class WebActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val server = intent.getStringExtra("server") ?: Prefs.server(this)
        if (server.isNullOrEmpty()) {
            toast("请先填写服务地址")
            finish()
            return
        }
        val web = WebView(this).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.allowFileAccess = false
            settings.loadWithOverviewMode = true
            settings.useWideViewPort = true
            webViewClient = WebViewClient()
            loadUrl(Prefs.withToken(this@WebActivity, server.trimEnd('/') + "/"))
        }
        setContentView(web, ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
    }

    @Deprecated("兼容旧版返回键")
    override fun onBackPressed() {
        val web = (findViewById<ViewGroup>(android.R.id.content).getChildAt(0)) as? WebView
        if (web != null && web.canGoBack()) web.goBack() else super.onBackPressed()
    }
}
