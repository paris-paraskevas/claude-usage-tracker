package com.claudeusage.tracker

import android.app.Application

class App : Application() {
    override fun onCreate() {
        super.onCreate()
        Notifications.createChannels(this)
    }
}
