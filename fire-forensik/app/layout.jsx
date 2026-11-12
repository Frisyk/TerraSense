import { Bricolage_Grotesque, IBM_Plex_Sans, IBM_Plex_Mono } from 'next/font/google';
import './globals.css';

const bricolage = Bricolage_Grotesque({
  subsets: ['latin'],
  weight: ['600', '700'],
  variable: '--font-bricolage',
  display: 'swap',
});

const ibmSans = IBM_Plex_Sans({
  subsets: ['latin'],
  weight: ['400', '500', '600'],
  variable: '--font-ibm-sans',
  display: 'swap',
});

const ibmMono = IBM_Plex_Mono({
  subsets: ['latin'],
  weight: ['400', '500'],
  variable: '--font-ibm-mono',
  display: 'swap',
});

export const metadata = {
  title: 'Jejak Hotspot Kalbar — Forensik Sebaran Api VIIRS 375 m',
  description:
    'Forensik sebaran api dari hotspot VIIRS 375 m Kalimantan Barat. Klaster spatio-temporal, titik awal, pohon panah sebaran, uji kestabilan, dan pertumbuhan area alpha shape.',
};

export default function RootLayout({ children }) {
  return (
    <html
      lang="id"
      className={`${bricolage.variable} ${ibmSans.variable} ${ibmMono.variable}`}
    >
      <body>{children}</body>
    </html>
  );
}

