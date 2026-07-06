import java.io.File
import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

// Apply Firebase's google-services plugin only when the config file is present, so the
// app still builds (Phase 1, no push) before you wire up Firebase. See README.md.
if (File(projectDir, "google-services.json").exists()) {
    apply(plugin = "com.google.gms.google-services")
}

// Release signing — credentials live in android/keystore.properties (gitignored).
// Without that file the project still builds debug; release signing is just skipped.
val keystorePropsFile = rootProject.file("keystore.properties")
val keystoreProps = Properties().apply {
    if (keystorePropsFile.exists()) keystorePropsFile.inputStream().use { load(it) }
}

android {
    namespace = "com.claudeusage.tracker"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.claudeusage.tracker"
        minSdk = 26
        targetSdk = 35
        versionCode = 15
        versionName = "0.2.0"
    }
    signingConfigs {
        if (keystorePropsFile.exists()) create("release") {
            storeFile = rootProject.file(keystoreProps["storeFile"] as String)
            storePassword = keystoreProps["storePassword"] as String
            keyAlias = keystoreProps["keyAlias"] as String
            keyPassword = keystoreProps["keyPassword"] as String
        }
    }
    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            if (keystorePropsFile.exists()) signingConfig = signingConfigs.getByName("release")
            ndk { debugSymbolLevel = "FULL" }   // bundle native debug symbols (libsodium/JNA) for Play
        }
    }
    buildFeatures {
        compose = true
        buildConfig = true
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    packaging { resources { excludes += "/META-INF/{AL2.0,LGPL2.1}" } }
}

dependencies {
    implementation(platform("androidx.compose:compose-bom:2024.09.03"))
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.7")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-graphics")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")

    // Home-screen widget (Glance) + background refresh (WorkManager)
    implementation("androidx.glance:glance-appwidget:1.1.0")
    implementation("androidx.work:work-runtime-ktx:2.9.1")

    implementation("androidx.security:security-crypto:1.1.0-alpha06")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")

    // libsodium — exact crypto_secretbox interop with PyNaCl on the desktop
    implementation("com.goterl:lazysodium-android:5.1.0@aar")
    implementation("net.java.dev.jna:jna:5.14.0@aar")

    // QR scanning
    implementation("com.journeyapps:zxing-android-embedded:4.3.0")

    // Push (FCM). Initialized manually in App.kt; google-services.json optional at build time.
    implementation(platform("com.google.firebase:firebase-bom:33.5.1"))
    implementation("com.google.firebase:firebase-messaging-ktx")
}
