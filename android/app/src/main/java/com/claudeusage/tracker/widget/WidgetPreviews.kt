package com.claudeusage.tracker.widget

import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProviderInfo
import android.content.ComponentName
import android.content.Context
import android.os.Build
import android.widget.RemoteViews
import androidx.annotation.RequiresApi
import androidx.datastore.preferences.core.emptyPreferences
import androidx.glance.appwidget.compose
import com.claudeusage.tracker.Prefs
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * Generated widget-picker previews (Android 15+): once an account is paired and has live data,
 * the picker shows this user's own widget (in their wallpaper colours) instead of the static
 * `previewLayout` (Android 12–14) / `previewImage` (older). The platform rate-limits
 * `setWidgetPreview` (about twice an hour), so it is set once per active account, retried at most
 * every 30 minutes until it sticks, and removed again on unpair.
 */
internal object WidgetPreviews {
    private const val RETRY_MS = 30 * 60 * 1000L

    @Volatile private var lastAttempt = 0L

    suspend fun maybePublish(ctx: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.VANILLA_ICE_CREAM) return
        val app = ctx.applicationContext
        val active = Prefs.activeId(app) ?: return
        if (WidgetData.previewFor(app) == active) return
        val now = System.currentTimeMillis()
        if (now - lastAttempt < RETRY_MS) return
        val ui = WidgetModel.load(app, active, now)
        if (ui.phase != Phase.LIVE) return              // only real, current numbers
        lastAttempt = now
        try {
            // Responsive mode without a size composes every bucket, so the picker can show the
            // 2×1, 2×2 and 4×2 layouts at whatever size it previews.
            val views = withContext(Dispatchers.Default) {
                UsageWidget(preview = ui).compose(app, state = emptyPreferences())
            }
            if (Api35.set(app, views)) WidgetData.setPreviewFor(app, active)
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            // Best effort: the static previewLayout / previewImage still work.
        }
    }

    /** Back to the static preview (everything was unpaired). */
    fun remove(ctx: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.VANILLA_ICE_CREAM) return
        runCatching { Api35.remove(ctx.applicationContext) }
        lastAttempt = 0L
    }

    @RequiresApi(Build.VERSION_CODES.VANILLA_ICE_CREAM)
    private object Api35 {
        private fun provider(ctx: Context) = ComponentName(ctx, UsageWidgetReceiver::class.java)

        fun set(ctx: Context, views: RemoteViews): Boolean =
            AppWidgetManager.getInstance(ctx).setWidgetPreview(
                provider(ctx), AppWidgetProviderInfo.WIDGET_CATEGORY_HOME_SCREEN, views,
            )

        fun remove(ctx: Context) =
            AppWidgetManager.getInstance(ctx).removeWidgetPreview(
                provider(ctx), AppWidgetProviderInfo.WIDGET_CATEGORY_HOME_SCREEN,
            )
    }
}
