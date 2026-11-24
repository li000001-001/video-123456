package com.vca.assistant

import android.content.Context
import android.widget.Toast

/** 轻量提示 */
fun Context.toast(msg: String) {
    Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
}

/** 本地配置：服务地址与访问口令 */
object Prefs {
    private const val SP = "vca_prefs"
    private const val K_SERVER = "server"
    private const val K_TOKEN = "token"

    private fun sp(ctx: Context) = ctx.getSharedPreferences(SP, Context.MODE_PRIVATE)

    fun server(ctx: Context): String = sp(ctx).getString(K_SERVER, "") ?: ""

    fun setServer(ctx: Context, value: String) {
        sp(ctx).edit().putString(K_SERVER, value).apply()
    }

    fun token(ctx: Context): String = sp(ctx).getString(K_TOKEN, "") ?: ""

    fun setToken(ctx: Context, value: String) {
        sp(ctx).edit().putString(K_TOKEN, value).apply()
    }

    /** 把访问口令拼进查询参数 */
    fun withToken(ctx: Context, url: String): String {
        val token = token(ctx)
        if (token.isEmpty()) return url
        val sep = if (url.contains("?")) "&" else "?"
        return url + sep + "k=" + java.net.URLEncoder.encode(token, "UTF-8")
    }
}
