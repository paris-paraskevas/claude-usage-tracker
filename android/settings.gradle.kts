pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()   // zxing-android-embedded, lazysodium-android, JNA all live here
    }
}

rootProject.name = "ClaudeUsageTracker"
include(":app")
