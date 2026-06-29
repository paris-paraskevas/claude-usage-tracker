package com.claudeusage.tracker.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.QrCodeScanner
import androidx.compose.material3.Button
import androidx.compose.material3.Icon
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.activity.compose.rememberLauncherForActivityResult
import com.claudeusage.tracker.Pairing
import com.claudeusage.tracker.Prefs
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions

@Composable
fun PairScreen(onPaired: () -> Unit) {
    val ctx = LocalContext.current
    var error by remember { mutableStateOf<String?>(null) }
    var code by remember { mutableStateOf("") }
    var manual by remember { mutableStateOf(false) }

    fun pairFrom(text: String) {
        val p = Pairing.parse(text.trim())
        if (p == null) error = "That isn't a valid pairing code." else { Prefs.save(ctx, p); onPaired() }
    }

    val scanLauncher = rememberLauncherForActivityResult(ScanContract()) { result ->
        val contents = result.contents
        if (contents == null) error = "Scan cancelled." else pairFrom(contents)
    }

    Column(
        modifier = Modifier.fillMaxSize().padding(28.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text("Usage Tracker for Claude", color = Ink, fontSize = 24.sp, textAlign = TextAlign.Center)
        Text("Track your plan limits on your phone", color = Dim, fontSize = 14.sp,
            textAlign = TextAlign.Center, modifier = Modifier.padding(top = 6.dp))
        Text(
            "On your desktop open the dashboard → Settings → Remote (phone), enable it, " +
                "then scan the pairing QR shown there.",
            color = Faint, fontSize = 13.sp, textAlign = TextAlign.Center,
            modifier = Modifier.padding(top = 18.dp, bottom = 24.dp),
        )
        Button(
            onClick = {
                error = null
                scanLauncher.launch(
                    ScanOptions()
                        .setOrientationLocked(false)
                        .setBeepEnabled(false)
                        .setPrompt("Scan the pairing QR from the desktop")
                )
            },
            shape = RoundedCornerShape(10.dp),
        ) {
            Icon(Icons.Filled.QrCodeScanner, contentDescription = null, modifier = Modifier.size(20.dp))
            Text("  Scan pairing QR")
        }

        TextButton(onClick = { manual = !manual; error = null }, modifier = Modifier.padding(top = 6.dp)) {
            Text(if (manual) "Hide manual entry" else "Enter pairing code manually", color = Dim)
        }
        if (manual) {
            OutlinedTextField(
                value = code,
                onValueChange = { code = it },
                placeholder = { Text("cutpair1:…") },
                singleLine = false,
                modifier = Modifier.fillMaxWidth(),
            )
            Button(
                onClick = { pairFrom(code) },
                shape = RoundedCornerShape(10.dp),
                modifier = Modifier.padding(top = 10.dp),
            ) { Text("Pair from code") }
        }

        error?.let {
            Text(it, color = hexColor("#d4694f"), fontSize = 13.sp,
                modifier = Modifier.padding(top = 16.dp))
        }
        Text(
            "Unofficial · not affiliated with Anthropic. \"Claude\" and \"Claude Code\" are trademarks of Anthropic.",
            color = Faint, fontSize = 11.sp, textAlign = TextAlign.Center,
            modifier = Modifier.padding(top = 28.dp),
        )
    }
}
