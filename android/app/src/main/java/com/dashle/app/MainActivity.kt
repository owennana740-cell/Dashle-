package com.dashle.app

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

private val DashleNavy = Color(0xFF050B16)
private val DashleGreen = Color(0xFF1FE05A)
private val DashleBlue = Color(0xFF0878FF)

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            MaterialTheme(colorScheme = darkColorScheme(
                primary = DashleGreen,
                secondary = DashleBlue,
                background = DashleNavy,
                surface = DashleNavy,
            )) {
                EcranInitial()
            }
        }
    }
}

@Composable
private fun EcranInitial() {
    Surface(modifier = Modifier.fillMaxSize(), color = DashleNavy) {
        Box(
            modifier = Modifier
                .fillMaxSize()
                .background(
                    Brush.verticalGradient(
                        colors = listOf(Color(0xFF0D1B2B), DashleNavy, Color(0xFF071325)),
                    ),
                )
                .padding(28.dp),
            contentAlignment = Alignment.Center,
        ) {
            Column(
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.Center,
            ) {
                Image(
                    painter = painterResource(R.drawable.dashle_icon),
                    contentDescription = "Logo DASHLE",
                    modifier = Modifier.size(184.dp),
                )
                Spacer(Modifier.height(12.dp))
                Text(
                    text = "DASHLE",
                    color = Color.White,
                    fontSize = 30.sp,
                    fontWeight = FontWeight.Bold,
                    letterSpacing = 5.sp,
                )
                Spacer(Modifier.height(10.dp))
                Box(
                    modifier = Modifier
                        .width(76.dp)
                        .height(3.dp)
                        .background(
                            Brush.horizontalGradient(listOf(DashleGreen, DashleBlue)),
                            RoundedCornerShape(50),
                        ),
                )
                Spacer(Modifier.height(22.dp))
                Text(
                    text = "La base de l’application Android est prête.",
                    color = Color(0xFFC3CFDD),
                    fontSize = 16.sp,
                    textAlign = TextAlign.Center,
                )
            }
        }
    }
}
