package com.claudeusage.tracker.ui

import com.journeyapps.barcodescanner.CaptureActivity

/**
 * zxing capture screen pinned to portrait. The lock comes from android:screenOrientation="portrait"
 * on this activity in AndroidManifest.xml — so scanning the pairing QR never rotates the phone to
 * landscape. Use with ScanOptions.setOrientationLocked(false).setCaptureActivity(PortraitCaptureActivity::class.java):
 * setOrientationLocked(true) would instead force the activity to the device's current orientation,
 * overriding the manifest and letting it flip to landscape (the old behaviour).
 */
class PortraitCaptureActivity : CaptureActivity()
