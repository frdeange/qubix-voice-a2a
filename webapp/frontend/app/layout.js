export const metadata = {
  title: "Qubix Voice",
  description: "Voice assistant with Work IQ (delegated user identity)",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body style={{ fontFamily: "system-ui, sans-serif", margin: 0, background: "#0f1220", color: "#e8eaf0" }}>
        {children}
      </body>
    </html>
  );
}
